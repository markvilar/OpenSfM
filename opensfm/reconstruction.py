# -*- coding: utf-8 -*-
"""Incremental reconstruction pipeline"""

import copy
import datetime
import logging
import math
from collections import defaultdict
from itertools import combinations

import cv2
import numpy as np
import networkx as nx
import six
from timeit import default_timer as timer
from six import iteritems

from opensfm import pybundle
from opensfm import pygeometry
from opensfm import align
from opensfm import log
from opensfm import tracking
from opensfm import multiview
from opensfm import types
from opensfm import pysfm
from opensfm import features
from opensfm.align import align_reconstruction, apply_similarity
from opensfm.context import parallel_map, current_memory_usage

from opensfm import pymap
from opensfm import pyslam
from opensfm.slam import slam_debug
# import slam_debug
logger = logging.getLogger(__name__)


def _add_camera_to_bundle(ba, camera, camera_prior, constant):
    """Add camera to a bundle adjustment problem."""
    if camera.projection_type == 'perspective':
        ba.add_perspective_camera(
            camera.id, camera.focal, camera.k1, camera.k2,
            camera_prior.focal, camera_prior.k1, camera_prior.k2,
            constant)
    elif camera.projection_type == 'brown':
        c = pybundle.BABrownPerspectiveCamera()
        c.id = camera.id
        c.focal_x = camera.focal_x
        c.focal_y = camera.focal_y
        c.c_x = camera.c_x
        c.c_y = camera.c_y
        c.k1 = camera.k1
        c.k2 = camera.k2
        c.p1 = camera.p1
        c.p2 = camera.p2
        c.k3 = camera.k3
        c.focal_x_prior = camera_prior.focal_x
        c.focal_y_prior = camera_prior.focal_y
        c.c_x_prior = camera_prior.c_x
        c.c_y_prior = camera_prior.c_y
        c.k1_prior = camera_prior.k1
        c.k2_prior = camera_prior.k2
        c.p1_prior = camera_prior.p1
        c.p2_prior = camera_prior.p2
        c.k3_prior = camera_prior.k3
        c.constant = constant
        ba.add_brown_perspective_camera(c)
    elif camera.projection_type == 'fisheye':
        ba.add_fisheye_camera(
            camera.id, camera.focal, camera.k1, camera.k2,
            camera_prior.focal, camera_prior.k1, camera_prior.k2,
            constant)
    elif camera.projection_type == 'dual':
        ba.add_dual_camera(
            camera.id, camera.focal, camera.k1, camera.k2,
            camera_prior.focal, camera_prior.k1, camera_prior.k2,
            camera.transition, constant)
    elif camera.projection_type in ['equirectangular', 'spherical']:
        ba.add_equirectangular_camera(camera.id)


def _get_camera_from_bundle(ba, camera):
    """Read camera parameters from a bundle adjustment problem."""
    if camera.projection_type == 'perspective':
        c = ba.get_perspective_camera(camera.id)
        camera.focal = c.focal
        camera.k1 = c.k1
        camera.k2 = c.k2
    elif camera.projection_type == 'brown':
        c = ba.get_brown_perspective_camera(camera.id)
        camera.focal_x = c.focal_x
        camera.focal_y = c.focal_y
        camera.c_x = c.c_x
        camera.c_y = c.c_y
        camera.k1 = c.k1
        camera.k2 = c.k2
        camera.p1 = c.p1
        camera.p2 = c.p2
        camera.k3 = c.k3
    elif camera.projection_type == 'fisheye':
        c = ba.get_fisheye_camera(camera.id)
        camera.focal = c.focal
        camera.k1 = c.k1
        camera.k2 = c.k2
    elif camera.projection_type == 'dual':
        c = ba.get_dual_camera(camera.id)
        camera.focal = c.focal
        camera.k1 = c.k1
        camera.k2 = c.k2
        camera.transition = c.transition


def triangulate_gcp(point, shots):
    """Compute the reconstructed position of a GCP from observations."""
    reproj_threshold = 1.0
    min_ray_angle = np.radians(0.1)

    os, bs, ids = [], [], []
    for observation in point.observations:
        shot_id = observation.shot_id
        if shot_id in shots:
            shot = shots[shot_id]
            os.append(shot.pose.get_origin())
            x = observation.projection
            b = shot.camera.pixel_bearing(np.array(x))
            r = shot.pose.get_rotation_matrix().T
            bs.append(r.dot(b))
            ids.append(shot_id)

    if len(os) >= 2:
        thresholds = len(os) * [reproj_threshold]
        e, X = pygeometry.triangulate_bearings_midpoint(
            os, bs, thresholds, min_ray_angle)
        return X


def _add_gcp_to_bundle(ba, gcp, shots):
    """Add Ground Control Points constraints to the bundle problem."""
    for point in gcp:
        point_id = 'gcp-' + point.id

        coordinates = triangulate_gcp(point, shots)
        if coordinates is None:
            if point.coordinates is not None:
                coordinates = point.coordinates
            else:
                logger.warning("Cannot initialize GCP '{}'."
                               "  Ignoring it".format(point.id))
                continue

        ba.add_point(point_id, coordinates, False)

        if point.coordinates is not None:
            point_type = pybundle.XYZ if point.has_altitude else pybundle.XY
            ba.add_point_position_world(point_id, point.coordinates, 0.1,
                                        point_type)

        for observation in point.observations:
            if observation.shot_id in shots:
                # TODO(pau): move this to a config or per point parameter.
                scale = 0.0001
                ba.add_point_projection_observation(
                    observation.shot_id,
                    point_id,
                    observation.projection[0],
                    observation.projection[1],
                    scale)


def bundle(graph, reconstruction, camera_priors, gcp, config):
    """Bundle adjust a reconstruction."""
    fix_cameras = not config['optimize_camera_parameters']

    chrono = Chronometer()
    ba = pybundle.BundleAdjuster()

    for camera in reconstruction.cameras.values():
        camera_prior = camera_priors[camera.id]
        _add_camera_to_bundle(ba, camera, camera_prior, fix_cameras)

    for shot in reconstruction.shots.values():
        r = shot.pose.rotation
        t = shot.pose.translation
        ba.add_shot(shot.id, shot.camera.id, r, t, False)

    for point in reconstruction.points.values():
        ba.add_point(point.id, point.coordinates, False)

    for shot_id in reconstruction.shots:
        if shot_id in graph:
            for track in graph[shot_id]:
                if track in reconstruction.points:
                    point = graph[shot_id][track]['feature']
                    scale = graph[shot_id][track]['feature_scale']
                    ba.add_point_projection_observation(
                        shot_id, track, point[0], point[1], scale)

    if config['bundle_use_gps']:
        for shot in reconstruction.shots.values():
            g = shot.metadata.gps_position
            ba.add_position_prior(shot.id, g[0], g[1], g[2],
                                  shot.metadata.gps_dop)

    if config['bundle_use_gcp'] and gcp:
        _add_gcp_to_bundle(ba, gcp, reconstruction.shots)

    align_method = config['align_method']
    if align_method == 'auto':
        align_method = align.detect_alignment_constraints(
            config, reconstruction, gcp)
    if align_method == 'orientation_prior':
        if config['align_orientation_prior'] == 'vertical':
            for shot_id in reconstruction.shots:
                ba.add_absolute_up_vector(shot_id, [0, 0, -1], 1e-3)
        if config['align_orientation_prior'] == 'horizontal':
            for shot_id in reconstruction.shots:
                ba.add_absolute_up_vector(shot_id, [0, -1, 0], 1e-3)

    ba.set_point_projection_loss_function(config['loss_function'],
                                          config['loss_function_threshold'])
    ba.set_internal_parameters_prior_sd(
        config['exif_focal_sd'],
        config['principal_point_sd'],
        config['radial_distorsion_k1_sd'],
        config['radial_distorsion_k2_sd'],
        config['radial_distorsion_p1_sd'],
        config['radial_distorsion_p2_sd'],
        config['radial_distorsion_k3_sd'])
    ba.set_num_threads(config['processes'])
    ba.set_max_num_iterations(config['bundle_max_iterations'])
    ba.set_linear_solver_type("SPARSE_SCHUR")

    chrono.lap('setup')
    ba.run()
    chrono.lap('run')

    for camera in reconstruction.cameras.values():
        _get_camera_from_bundle(ba, camera)

    for shot in reconstruction.shots.values():
        s = ba.get_shot(shot.id)
        shot.pose.rotation = [s.r[0], s.r[1], s.r[2]]
        shot.pose.translation = [s.t[0], s.t[1], s.t[2]]

    for point in reconstruction.points.values():
        p = ba.get_point(point.id)
        point.coordinates = [p.p[0], p.p[1], p.p[2]]
        point.reprojection_errors = p.reprojection_errors

    chrono.lap('teardown')

    logger.debug(ba.brief_report())
    report = {
        'wall_times': dict(chrono.lap_times()),
        'brief_report': ba.brief_report(),
    }
    return report


def bundle_single_view(reconstruction: pymap.Map, shot_id, camera, camera_priors, config):
    """Bundle adjust a single camera."""
    ba = pybundle.BundleAdjuster()
    shot: pymap.Shot = reconstruction.get_shot(shot_id)
    # TODO: Get camera from shot
    # For now assume that there is one camera for everything
    camera_prior = camera_priors[camera.id]
    _add_camera_to_bundle(ba, camera, camera_prior, constant=True)
    shot_pose: pymap.Pose = shot.get_pose()
    ba.add_shot(str(shot_id), camera.id, shot_pose.get_R_world_to_cam_min(),
                shot_pose.get_t_world_to_cam(), False)
    lms = shot.get_valid_landmarks()
    for lm in lms:
        ba.add_point(str(lm.id), lm.get_global_pos(), True)
        obs = lm.get_obs_in_shot(shot)
        
        # TODO: Normalize!
        obs, _, _ = features.normalize_features(np.reshape(
            obs, [1, 3]), None, None, camera.width, camera.height)
        ba.add_point_projection_observation(
            str(shot_id), str(lm.id), obs[0, 0], obs[0, 1], obs[0, 2])

    if config['bundle_use_gps']:
        g = shot.shot_measurement.gps_pos
        ba.add_position_prior(shot_id, g[0], g[1], g[2],
                              shot.shot_measurement.gps_dop)

    ba.set_point_projection_loss_function(config['loss_function'],
                                          config['loss_function_threshold'])
    ba.set_internal_parameters_prior_sd(
        config['exif_focal_sd'],
        config['principal_point_sd'],
        config['radial_distorsion_k1_sd'],
        config['radial_distorsion_k2_sd'],
        config['radial_distorsion_p1_sd'],
        config['radial_distorsion_p2_sd'],
        config['radial_distorsion_k3_sd'])
    ba.set_num_threads(config['processes'])
    ba.set_max_num_iterations(10)
    ba.set_linear_solver_type("DENSE_QR")
    ba.run()
    # print(ba.full_report())
    # logger.debug(ba.brief_report())
    # TODO: uncomment
    s = ba.get_shot(shot_id)
    new_pose = pymap.Pose()
    new_pose.set_from_world_to_cam(s.r, s.t)
    shot.set_pose(new_pose)


def bundle_local(graph, reconstruction, camera_priors, gcp, central_shot_id, config):
    """Bundle adjust the local neighborhood of a shot."""
    chrono = Chronometer()

    interior, boundary = shot_neighborhood(
        graph, reconstruction, central_shot_id,
        config['local_bundle_radius'],
        config['local_bundle_min_common_points'],
        config['local_bundle_max_shots'])

    logger.debug(
        'Local bundle sets: interior {}  boundary {}  other {}'.format(
            len(interior), len(boundary),
            len(reconstruction.shots) - len(interior) - len(boundary)))

    point_ids = set()
    for shot_id in interior:
        if shot_id in graph:
            for track in graph[shot_id]:
                if track in reconstruction.points:
                    point_ids.add(track)

    ba = pybundle.BundleAdjuster()

    for camera in reconstruction.cameras.values():
        camera_prior = camera_priors[camera.id]
        _add_camera_to_bundle(ba, camera, camera_prior, constant=True)

    for shot_id in interior | boundary:
        shot = reconstruction.shots[shot_id]
        r = shot.pose.rotation
        t = shot.pose.translation
        ba.add_shot(shot.id, shot.camera.id, r, t, shot.id in boundary)

    for point_id in point_ids:
        point = reconstruction.points[point_id]
        ba.add_point(point.id, point.coordinates, False)

    for shot_id in interior | boundary:
        if shot_id in graph:
            for track in graph[shot_id]:
                if track in point_ids:
                    point = graph[shot_id][track]['feature']
                    scale = graph[shot_id][track]['feature_scale']
                    ba.add_point_projection_observation(
                        shot_id, track, point[0], point[1], scale)

    if config['bundle_use_gps']:
        for shot_id in interior:
            shot = reconstruction.shots[shot_id]
            g = shot.metadata.gps_position
            ba.add_position_prior(shot.id, g[0], g[1], g[2],
                                  shot.metadata.gps_dop)

    if config['bundle_use_gcp'] and gcp:
        _add_gcp_to_bundle(ba, gcp, reconstruction.shots)

    ba.set_point_projection_loss_function(config['loss_function'],
                                          config['loss_function_threshold'])
    ba.set_internal_parameters_prior_sd(
        config['exif_focal_sd'],
        config['principal_point_sd'],
        config['radial_distorsion_k1_sd'],
        config['radial_distorsion_k2_sd'],
        config['radial_distorsion_p1_sd'],
        config['radial_distorsion_p2_sd'],
        config['radial_distorsion_k3_sd'])
    ba.set_num_threads(config['processes'])
    ba.set_max_num_iterations(10)
    ba.set_linear_solver_type("DENSE_SCHUR")

    chrono.lap('setup')
    ba.run()
    chrono.lap('run')

    for shot_id in interior:
        shot = reconstruction.shots[shot_id]
        s = ba.get_shot(shot.id)
        shot.pose.rotation = [s.r[0], s.r[1], s.r[2]]
        shot.pose.translation = [s.t[0], s.t[1], s.t[2]]

    for point in point_ids:
        point = reconstruction.points[point]
        p = ba.get_point(point.id)
        point.coordinates = [p.p[0], p.p[1], p.p[2]]
        point.reprojection_errors = p.reprojection_errors

    chrono.lap('teardown')

    logger.debug(ba.brief_report())
    report = {
        'wall_times': dict(chrono.lap_times()),
        'brief_report': ba.brief_report(),
        'num_interior_images': len(interior),
        'num_boundary_images': len(boundary),
        'num_other_images': (len(reconstruction.shots)
                             - len(interior) - len(boundary)),
    }
    return point_ids, report


def shot_neighborhood(graph, reconstruction, central_shot_id, radius,
                      min_common_points, max_interior_size):
    """Reconstructed shots near a given shot.

    Returns:
        a tuple with interior and boundary:
        - interior: the list of shots at distance smaller than radius
        - boundary: shots sharing at least on point with the interior

    Central shot is at distance 0.  Shots at distance n + 1 share at least
    min_common_points points with shots at distance n.
    """
    max_boundary_size = 1000000
    interior = set([central_shot_id])
    for distance in range(1, radius):
        remaining = max_interior_size - len(interior)
        if remaining <= 0:
            break
        neighbors = direct_shot_neighbors(
            graph, reconstruction, interior, min_common_points, remaining)
        interior.update(neighbors)
    boundary = direct_shot_neighbors(
        graph, reconstruction, interior, 1, max_boundary_size)
    return interior, boundary


def direct_shot_neighbors(graph, reconstruction, shot_ids,
                          min_common_points, max_neighbors):
    """Reconstructed shots sharing reconstructed points with a shot set."""
    points = set()
    for shot_id in shot_ids:
        for track_id in graph[shot_id]:
            if track_id in reconstruction.points:
                points.add(track_id)

    candidate_shots = set(reconstruction.shots) - set(shot_ids)
    common_points = defaultdict(int)
    for track_id in points:
        for neighbor in graph[track_id]:
            if neighbor in candidate_shots:
                common_points[neighbor] += 1

    pairs = sorted(common_points.items(), key=lambda x: -x[1])
    neighbors = set()
    for neighbor, num_points in pairs[:max_neighbors]:
        if num_points >= min_common_points:
            neighbors.add(neighbor)
        else:
            break
    return neighbors


def pairwise_reconstructability(common_tracks, rotation_inliers):
    """Likeliness of an image pair giving a good initial reconstruction."""
    outliers = common_tracks - rotation_inliers
    outlier_ratio = float(outliers) / common_tracks
    if outlier_ratio >= 0.3:
        return outliers
    else:
        return 0


def compute_image_pairs(track_dict, cameras, data):
    """All matched image pairs sorted by reconstructability."""
    args = _pair_reconstructability_arguments(track_dict, cameras, data)
    processes = data.config['processes']
    result = parallel_map(_compute_pair_reconstructability, args, processes)
    result = list(result)
    pairs = [(im1, im2) for im1, im2, r in result if r > 0]
    score = [r for im1, im2, r in result if r > 0]
    order = np.argsort(-np.array(score))
    return [pairs[o] for o in order]


def _pair_reconstructability_arguments(track_dict, cameras, data):
    threshold = 4 * data.config['five_point_algo_threshold']
    args = []
    for (im1, im2), (tracks, p1, p2) in iteritems(track_dict):
        camera1 = cameras[data.load_exif(im1)['camera']]
        camera2 = cameras[data.load_exif(im2)['camera']]
        args.append((im1, im2, p1, p2, camera1, camera2, threshold))
    return args


def _compute_pair_reconstructability(args):
    log.setup()
    im1, im2, p1, p2, camera1, camera2, threshold = args
    R, inliers = two_view_reconstruction_rotation_only(
        p1, p2, camera1, camera2, threshold)
    r = pairwise_reconstructability(len(p1), len(inliers))
    return (im1, im2, r)


def get_image_metadata(data, image):
    """Get image metadata as a ShotMetadata object."""
    metadata = types.ShotMetadata()
    exif = data.load_exif(image)
    reference = data.load_reference()
    if ('gps' in exif and
            'latitude' in exif['gps'] and
            'longitude' in exif['gps']):
        lat = exif['gps']['latitude']
        lon = exif['gps']['longitude']
        if data.config['use_altitude_tag']:
            alt = exif['gps'].get('altitude', 2.0)
        else:
            alt = 2.0  # Arbitrary value used to align the reconstruction
        x, y, z = reference.to_topocentric(lat, lon, alt)
        metadata.gps_position = [x, y, z]
        metadata.gps_dop = exif['gps'].get('dop', 15.0)
        if metadata.gps_dop == 0.0:
            metadata.gps_dop = 15.0
    else:
        metadata.gps_position = [0.0, 0.0, 0.0]
        metadata.gps_dop = 999999.0

    metadata.orientation = exif.get('orientation', 1)

    if 'accelerometer' in exif:
        metadata.accelerometer = exif['accelerometer']

    if 'compass' in exif:
        metadata.compass = exif['compass']

    if 'capture_time' in exif:
        metadata.capture_time = exif['capture_time']

    if 'skey' in exif:
        metadata.skey = exif['skey']

    return metadata


def _two_view_reconstruction_inliers(b1, b2, R, t, threshold):
    """Compute number of points that can be triangulated.

    Args:
        b1, b2: Bearings in the two images.
        R, t: Rotation and translation from the second image to the first.
              That is the convention and the opposite of many
              functions in this module.
        threshold: max reprojection error in radians.
    Returns:
        array: Inlier indices.
    """
    p = np.array(
        pygeometry.triangulate_two_bearings_midpoint_many(b1, b2, R, t))

    br1 = p.copy()
    br1 /= np.linalg.norm(br1, axis=1)[:, np.newaxis]

    br2 = R.T.dot((p - t).T).T
    br2 /= np.linalg.norm(br2, axis=1)[:, np.newaxis]

    ok1 = np.linalg.norm(br1 - b1, axis=1) < threshold
    ok2 = np.linalg.norm(br2 - b2, axis=1) < threshold
    return np.nonzero(ok1 * ok2)[0]


def two_view_reconstruction_plane_based(p1, p2, camera1, camera2, threshold):
    """Reconstruct two views from point correspondences lying on a plane.

    Args:
        p1, p2: lists points in the images
        camera1, camera2: Camera models
        threshold: reprojection error threshold

    Returns:
        rotation, translation and inlier list
    """
    b1 = camera1.pixel_bearing_many(p1)
    b2 = camera2.pixel_bearing_many(p2)
    x1 = multiview.euclidean(b1)
    x2 = multiview.euclidean(b2)

    H, inliers = cv2.findHomography(x1, x2, cv2.RANSAC, threshold)
    motions = multiview.motion_from_plane_homography(H)

    if len(motions) == 0:
        return None, None, []

    motion_inliers = []
    for R, t, n, d in motions:
        inliers = _two_view_reconstruction_inliers(
            b1, b2, R.T, -R.T.dot(t), threshold)
        motion_inliers.append(inliers)

    best = np.argmax(map(len, motion_inliers))
    R, t, n, d = motions[best]
    inliers = motion_inliers[best]
    return cv2.Rodrigues(R)[0].ravel(), t, inliers


def two_view_reconstruction(p1, p2, camera1, camera2,
                            threshold, iterations):
    """Reconstruct two views using the 5-point method.

    Args:
        p1, p2: lists points in the images
        camera1, camera2: Camera models
        threshold: reprojection error threshold

    Returns:
        rotation, translation and inlier list
    """
    b1 = camera1.pixel_bearing_many(p1)
    b2 = camera2.pixel_bearing_many(p2)

    T = multiview.relative_pose_ransac(
        b1, b2, threshold, 1000, 0.999)
    R = T[:, :3]
    t = T[:, 3]
    inliers = _two_view_reconstruction_inliers(b1, b2, R, t, threshold)

    if inliers.sum() > 5:
        T = multiview.relative_pose_optimize_nonlinear(b1[inliers],
                                                       b2[inliers],
                                                       t, R,
                                                       iterations)
        R = T[:, :3]
        t = T[:, 3]
        inliers = _two_view_reconstruction_inliers(b1, b2, R, t, threshold)

    return cv2.Rodrigues(R.T)[0].ravel(), -R.T.dot(t), inliers


def _two_view_rotation_inliers(b1, b2, R, threshold):
    br2 = R.dot(b2.T).T
    ok = np.linalg.norm(br2 - b1, axis=1) < threshold
    return np.nonzero(ok)[0]


def two_view_reconstruction_rotation_only(p1, p2, camera1, camera2, threshold):
    """Find rotation between two views from point correspondences.

    Args:
        p1, p2: lists points in the images
        camera1, camera2: Camera models
        threshold: reprojection error threshold

    Returns:
        rotation and inlier list
    """
    b1 = camera1.pixel_bearing_many(p1)
    b2 = camera2.pixel_bearing_many(p2)

    R = multiview.relative_pose_ransac_rotation_only(
        b1, b2, threshold, 1000, 0.999)
    inliers = _two_view_rotation_inliers(b1, b2, R, threshold)

    return cv2.Rodrigues(R.T)[0].ravel(), inliers


def two_view_reconstruction_general(p1, p2, camera1, camera2,
                                    threshold, iterations):
    """Reconstruct two views from point correspondences.

    These will try different reconstruction methods and return the
    results of the one with most inliers.

    Args:
        p1, p2: lists points in the images
        camera1, camera2: Camera models
        threshold: reprojection error threshold

    Returns:
        rotation, translation and inlier list
    """
    R_5p, t_5p, inliers_5p = two_view_reconstruction(
        p1, p2, camera1, camera2, threshold, iterations)

    R_plane, t_plane, inliers_plane = two_view_reconstruction_plane_based(
        p1, p2, camera1, camera2, threshold)

    report = {
        '5_point_inliers': len(inliers_5p),
        'plane_based_inliers': len(inliers_plane),
    }

    if len(inliers_5p) > len(inliers_plane):
        report['method'] = '5_point'
        return R_5p, t_5p, inliers_5p, report
    else:
        report['method'] = 'plane_based'
        return R_plane, t_plane, inliers_plane, report


def bootstrap_reconstruction(data, tracks_manager, reconstruction, camera_priors, im1, im2, p1, p2):
    """Start a reconstruction using two shots."""
    logger.info("Starting reconstruction with {} and {}".format(im1, im2))
    report = {
        'image_pair': (im1, im2),
        'common_tracks': len(p1),
    }

    camera_id1 = data.load_exif(im1)['camera']
    camera_id2 = data.load_exif(im2)['camera']
    camera1 = camera_priors[camera_id1]
    camera2 = camera_priors[camera_id2]

    threshold = data.config['five_point_algo_threshold']
    min_inliers = data.config['five_point_algo_min_inliers']
    iterations = data.config['five_point_refine_rec_iterations']
    chrono = slam_debug.Chronometer()
    R, t, inliers, report['two_view_reconstruction'] = \
        two_view_reconstruction_general(
            p1, p2, camera1, camera2, threshold, iterations)
    chrono.lap('two_view_rec')
    logger.info("Two-view reconstruction inliers: {} / {}".format(
        len(inliers), len(p1)))

    if len(inliers) <= 5:
        report['decision'] = "Could not find initial motion"
        logger.info(report['decision'])
        return False, report

    #We always add the shot
    shot1 = reconstruction.get_shot(im1)
    if shot1 is None:
        shot1_id = reconstruction.next_unique_shot_id()
        shot1 = reconstruction.create_shot(shot1_id, camera1.id, im1)
        metadata = get_image_metadata(data, im1)
        shot1.shot_measurement.gps_dop = metadata.gps_dop
        shot1.shot_measurement.gps_pos = metadata.gps_position
        # shot1 = get_image_metadata
        # metadata
    shot1.set_pose(pymap.Pose())
    # same with frame2

    shot2 = reconstruction.get_shot(im2)
    if shot2 is None:
        shot2_id = reconstruction.next_unique_shot_id()
        shot2 = reconstruction.create_shot(shot2_id, camera2.id, im2)
        metadata = get_image_metadata(data, im2)
        shot2.shot_measurement.gps_dop = metadata.gps_dop
        shot2.shot_measurement.gps_pos = metadata.gps_position
        # metdata
        # pose  
    pose = pymap.Pose()
    pose.set_from_world_to_cam(R, t)
    shot2.set_pose(pose)

    # curr_shot.shot_measurement.gps_dop = metadata.gps_dop
    # curr_shot.shot_measurement.gps_pos = metadata.gps_position
    # shot2 = reconstruction.get_shot(im2)
    # shot2_pose = pymap.Pose()
    # shot2_pose.set_from_world_to_cam(R, t)
    # shot2.set_pose(shot2_pose)

    reproj_threshold = data.config['triangulation_threshold']
    min_ray_angle = data.config['triangulation_min_ray_angle']
    shot = reconstruction.get_shot(im1)
    # chrono.start()
    # triangulate_shot_features(
    #     tracks_manager, reconstruction, im1, data.config, camera1)
    # chrono.lap('old_tri')
    # lms = reconstruction.get_all_landmarks()
    # for lm_id in sorted(lms.keys()):
    #     lm = lms[lm_id]
    #     print(lm_id, ":", lm.get_global_pos())
    # chrono.lap('print')
    # reconstruction.clear_observations_and_landmarks()
    # print('cleared!')
    # chrono.lap('clear')
    chrono.lap('new')
    pyslam.SlamUtilities.triangulate_shot_features(
        tracks_manager, reconstruction, shot, reproj_threshold, np.radians(min_ray_angle))
    chrono.lap('triangulate_shot_features')
    # lms = reconstruction.get_all_landmarks()
    # for lm_id in sorted(lms.keys()):
    #     lm = lms[lm_id]
    #     print(lm_id, ":", lm.get_global_pos())
    # chrono.lap('print2')
    # print(chrono.lap_times())
    # exit(0)
    chrono.lap('triangulate_shot_features')
    logger.info("Triangulated: {}".format(
        reconstruction.number_of_landmarks()))
    report['triangulated_points'] = reconstruction.number_of_landmarks()

    if reconstruction.number_of_landmarks() < min_inliers:
        report['decision'] = "Initial motion did not generate enough points"
        logger.info(report['decision'])
        return False, report
    chrono.lap('kk')

    new_pose = pyslam.SlamUtilities.bundle_single_view(shot2)
    shot2.set_pose(new_pose)
    chrono.lap('bef_bundle')

    # chrono.lap('new_bundle')
    # bundle_single_view(reconstruction, im2, camera1,
    #                    camera_priors, data.config)
    # chrono.lap('old_bundle')
    # chrono.start()
    # retriangulate(tracks_manager, reconstruction, data.config, camera1)
    # chrono.lap('retriangulate')
    # lms = reconstruction.get_all_landmarks()
    # for lm_id in sorted(lms.keys()):
    #     lm = lms[lm_id]
    #     print(lm_id, ":", lm.get_global_pos())
    # chrono.lap('print')
    pyslam.SlamUtilities.retriangulate(tracks_manager, reconstruction, reproj_threshold, np.radians(min_ray_angle), True)
    chrono.lap('new_retriangulate')
    # lms = reconstruction.get_all_landmarks()
    # for lm_id in sorted(lms.keys()):
    #     lm = lms[lm_id]
    #     print(lm_id, ":", lm.get_global_pos())
    # chrono.lap('print2')
    # print("timings: ", chrono.lap_times())
    logger.info("Retriangulated: {}".format(
        reconstruction.number_of_landmarks()))
    if reconstruction.number_of_landmarks() < min_inliers:
        report['decision'] = "Re-triangulation after initial motion did not generate enough points"
        logger.info(report['decision'])
        return False, report

    # chrono.start()
    chrono.lap('bef_bundl2')
    new_pose = pyslam.SlamUtilities.bundle_single_view(shot2)
    shot2.set_pose(new_pose)
    chrono.lap('new_bundle_2')
    # bundle_single_view(reconstruction, im2, camera1,
    #                    camera_priors, data.config)
    # chrono.lap('old_bundle')
    print("timings: ", chrono.lap_times())
    report['decision'] = 'Success'
    report['memory_usage'] = current_memory_usage()
    return True, report


def reconstructed_points_for_images(tracks_manager, reconstruction, images):
    """Number of reconstructed points visible on each image.

    Returns:
        A list of (image, num_point) pairs sorted by decreasing number
        of points.
    """
    non_reconstructed = [im for im in images if im not in reconstruction.shots]
    res = pysfm.count_tracks_per_shot(
        tracks_manager, non_reconstructed, list(reconstruction.points.keys()))
    return sorted(res.items(), key=lambda x: -x[1])


def resect(tracks_manager, graph_inliers, reconstruction, shot_id,
           camera, metadata, threshold, min_inliers):
    """Try resecting and adding a shot to the reconstruction.

    Return:
        True on success.
    """

    bs, Xs, ids = [], [], []
    for track, obs in tracks_manager.get_shot_observations(shot_id).items():
        if track in reconstruction.points:
            b = camera.pixel_bearing(obs.point)
            bs.append(b)
            Xs.append(reconstruction.points[track].coordinates)
            ids.append(track)
    bs = np.array(bs)
    Xs = np.array(Xs)
    if len(bs) < 5:
        return False, {'num_common_points': len(bs)}

    T = multiview.absolute_pose_ransac(
        bs, Xs, threshold, 1000, 0.999)

    R = T[:, :3]
    t = T[:, 3]

    reprojected_bs = R.T.dot((Xs - t).T).T
    reprojected_bs /= np.linalg.norm(reprojected_bs, axis=1)[:, np.newaxis]

    inliers = np.linalg.norm(reprojected_bs - bs, axis=1) < threshold
    ninliers = int(sum(inliers))

    logger.info("{} resection inliers: {} / {}".format(
        shot_id, ninliers, len(bs)))
    report = {
        'num_common_points': len(bs),
        'num_inliers': ninliers,
    }
    if ninliers >= min_inliers:
        R = T[:, :3].T
        t = -R.dot(T[:, 3])
        shot = types.Shot()
        shot.id = shot_id
        shot.camera = camera
        shot.pose = types.Pose()
        shot.pose.set_rotation_matrix(R)
        shot.pose.translation = t
        shot.metadata = metadata
        reconstruction.add_shot(shot)
        for i, succeed in enumerate(inliers):
            if succeed:
                copy_graph_data(tracks_manager, graph_inliers, shot_id, ids[i])
        return True, report
    else:
        return False, report


def corresponding_tracks(tracks1, tracks2):
    features1 = {obs.id: t1 for t1, obs in tracks1.items()}
    corresponding_tracks = []
    for t2, obs in tracks2.items():
        feature_id = obs.id
        if feature_id in features1:
            corresponding_tracks.append((features1[feature_id], t2))
    return corresponding_tracks


def compute_common_tracks(reconstruction1, reconstruction2,
                          tracks_manager1, tracks_manager2):
    common_tracks = set()
    common_images = set(reconstruction1.shots.keys()).intersection(
        reconstruction2.shots.keys())

    all_shot_ids1 = set(tracks_manager1.get_shot_ids())
    all_shot_ids2 = set(tracks_manager2.get_shot_ids())
    for image in common_images:
        if image not in all_shot_ids1 or image not in all_shot_ids2:
            continue
        at_shot1 = tracks_manager1.get_shot_observations(image)
        at_shot2 = tracks_manager2.get_shot_observations(image)
        for t1, t2 in corresponding_tracks(at_shot1, at_shot2):
            if t1 in reconstruction1.points and t2 in reconstruction2.points:
                common_tracks.add((t1, t2))
    return list(common_tracks)


def resect_reconstruction(reconstruction1, reconstruction2, tracks_manager1,
                          tracks_manager2, threshold, min_inliers):

    common_tracks = compute_common_tracks(
        reconstruction1, reconstruction2, tracks_manager1, tracks_manager2)
    worked, similarity, inliers = align_two_reconstruction(
        reconstruction1, reconstruction2, common_tracks, threshold)
    if not worked:
        return False, [], []

    inliers = [common_tracks[inliers[i]] for i in range(len(inliers))]
    return True, similarity, inliers


def copy_graph_data(tracks_manager, graph_inliers, shot_id, track_id):
    if shot_id not in graph_inliers:
        graph_inliers.add_node(shot_id, bipartite=0)
    if track_id not in graph_inliers:
        graph_inliers.add_node(track_id, bipartite=1)
    observation = tracks_manager.get_observation(shot_id, track_id)
    graph_inliers.add_edge(shot_id, track_id,
                           feature=observation.point,
                           feature_scale=observation.scale,
                           feature_id=observation.id,
                           feature_color=observation.color)


class TrackTriangulator:
    """Triangulate tracks in a reconstruction.

    Caches shot origin and rotation matrix
    """

    def __init__(self, tracks_manager, reconstruction):
        """Build a triangulator for a specific reconstruction."""
        self.tracks_manager = tracks_manager
        self.reconstruction: pymap.Map = reconstruction
        self.origins = {}
        self.rotation_inverses = {}
        self.Rts = {}
        self.print_shots = {}

    def triangulate_robust(self, track, reproj_threshold, min_ray_angle_degrees):
        """Triangulate track in a RANSAC way and add point to reconstruction."""
        os, bs, ids = [], [], []
        for shot_id, obs in self.tracks_manager.get_track_observations(track).items():
            if shot_id in self.reconstruction.shots:
                shot = self.reconstruction.shots[shot_id]
                os.append(self._shot_origin(shot))
                b = shot.camera.pixel_bearing(np.array(obs.point))
                r = self._shot_rotation_inverse(shot)
                bs.append(r.dot(b))
                ids.append(shot_id)

        if len(ids) < 2:
            return

        best_inliers = []
        best_point = types.Point()
        best_point.id = track

        combinatiom_tried = set()
        ransac_tries = 11  # 0.99 proba, 60% inliers
        all_combinations = list(combinations(range(len(ids)), 2))

        thresholds = len(os) * [reproj_threshold]
        for i in range(ransac_tries):
            random_id = int(np.random.rand() * (len(all_combinations) - 1))
            if random_id in combinatiom_tried:
                continue

            i, j = all_combinations[random_id]
            combinatiom_tried.add(random_id)

            os_t = [os[i], os[j]]
            bs_t = [bs[i], bs[j]]

            e, X = pygeometry.triangulate_bearings_midpoint(
                os_t, bs_t, thresholds, np.radians(min_ray_angle_degrees))

            if X is not None:
                reprojected_bs = X - os
                reprojected_bs /= np.linalg.norm(reprojected_bs,
                                                 axis=1)[:, np.newaxis]
                inliers = np.linalg.norm(
                    reprojected_bs - bs, axis=1) < reproj_threshold

                if sum(inliers) > sum(best_inliers):
                    best_inliers = inliers
                    best_point.coordinates = X.tolist()

                    pout = 0.99
                    inliers_ratio = float(sum(best_inliers)) / len(ids)
                    if inliers_ratio == 1.0:
                        break
                    optimal_iter = math.log(
                        1.0 - pout) / math.log(1.0 - inliers_ratio * inliers_ratio)
                    if optimal_iter <= ransac_tries:
                        break

        if len(best_inliers) > 1:
            self.reconstruction.add_point(best_point)
            for i, succeed in enumerate(best_inliers):
                if succeed:
                    self._add_track_to_graph_inlier(track, ids[i])

    def triangulate(self, track, reproj_threshold, min_ray_angle_degrees, camera):
        """Triangulate track and add point to reconstruction."""
        os, bs, ids = [], [], []
        # TODO: don't load the shot every time!
        # TODO: store it in a dict and maybe with its pose
        chrono = slam_debug.Chronometer()
        for shot_id, obs in self.tracks_manager.get_track_observations(track).items():
            shot = self.reconstruction.get_shot(shot_id)
            if shot is not None:
                shot_pose: pymap.Pose = shot.get_pose()
                os.append(shot_pose.get_origin())
                #convert to bearing
                #TODO handle multiple camera models
                b = camera.pixel_bearing(np.array(obs.point))
                r = shot_pose.get_R_cam_to_world()
                
                bs.append(r.dot(b))
                ids.append((shot_id, obs.id))
        # print(track, ": ", bs, os)
        # chrono.lap('bearing')
        if len(os) >= 2:
            thresholds = len(os) * [reproj_threshold]
            e, X = pygeometry.triangulate_bearings_midpoint(
                os, bs, thresholds, np.radians(min_ray_angle_degrees))
            # print("status: ", e)
            if X is not None:
                lm = self.reconstruction.create_landmark(
                    int(track), X.tolist())
                for shot_id, feat_id in ids:
                    shot = self.reconstruction.get_shot(shot_id)
                    self.reconstruction.add_observation(shot, lm, feat_id)
        # chrono.lap('pygeo')
        # print("chrono.laps: ", chrono.lap_times())

    def triangulate_dlt(self, track, reproj_threshold, min_ray_angle_degrees):
        """Triangulate track using DLT and add point to reconstruction."""
        Rts, bs, ids = [], [], []
        for shot_id, obs in self.tracks_manager.get_track_observations(track).items():
            if shot_id in self.reconstruction.shots:
                shot = self.reconstruction.shots[shot_id]
                Rts.append(self._shot_Rt(shot))
                b = shot.camera.pixel_bearing(np.array(obs.point))
                bs.append(b)
                ids.append(shot_id)

        if len(Rts) >= 2:
            e, X = pygeometry.triangulate_bearings_dlt(
                Rts, bs, reproj_threshold, np.radians(min_ray_angle_degrees))
            if X is not None:
                point = types.Point()
                point.id = track
                point.coordinates = X.tolist()
                self.reconstruction.add_point(point)
                for shot_id in ids:
                    self._add_track_to_graph_inlier(track, shot_id)

    def _add_track_to_graph_inlier(self, track_id, shot_id):
        copy_graph_data(self.tracks_manager,
                        self.graph_inliers, shot_id, track_id)

    def _shot_origin(self, shot):
        if shot.id in self.origins:
            return self.origins[shot.id]
        else:
            o = shot.pose.get_origin()
            self.origins[shot.id] = o
            return o

    def _shot_rotation_inverse(self, shot):
        if shot.id in self.rotation_inverses:
            return self.rotation_inverses[shot.id]
        else:
            r = shot.pose.get_rotation_matrix().T
            self.rotation_inverses[shot.id] = r
            return r

    def _shot_Rt(self, shot):
        if shot.id in self.Rts:
            return self.Rts[shot.id]
        else:
            r = shot.pose.get_Rt()
            self.Rts[shot.id] = r
            return r


def triangulate_shot_features(tracks_manager, reconstruction, shot_id, config, camera):
    """Reconstruct as many tracks seen in shot_id as possible."""
    reproj_threshold = config['triangulation_threshold']
    min_ray_angle = config['triangulation_min_ray_angle']

    triangulator = TrackTriangulator(tracks_manager, reconstruction)

    for track in tracks_manager.get_shot_observations(shot_id):
        if not reconstruction.has_landmark(int(track)):
            triangulator.triangulate(
                track, reproj_threshold, min_ray_angle, camera)


def retriangulate(tracks_manager, reconstruction: pymap.Map, config, camera):
    """Retrianguate all points"""
    chrono = Chronometer()
    report = {}
    # len(reconstruction.points)
    report['num_points_before'] = reconstruction.number_of_landmarks()

    threshold = config['triangulation_threshold']
    min_ray_angle = config['triangulation_min_ray_angle']

    reconstruction.clear_observations_and_landmarks()
    all_shots_ids = tracks_manager.get_shot_ids()

    triangulator = TrackTriangulator(tracks_manager, reconstruction)
    tracks = set()
    for shot_id in reconstruction.get_all_shots():
        shot = reconstruction.get_shot(shot_id)
        image = shot.name
        if image in all_shots_ids:
            tracks.update(tracks_manager.get_shot_observations(image).keys())
    for track in tracks:
        if config['triangulation_type'] == 'ROBUST':
            triangulator.triangulate_robust(track, threshold, min_ray_angle)
        elif config['triangulation_type'] == 'FULL':
            triangulator.triangulate(track, threshold, min_ray_angle, camera)

    # len(reconstruction.points)
    report['num_points_after'] = reconstruction.number_of_landmarks()
    chrono.lap('retriangulate')
    report['wall_time'] = chrono.total_time()
    return report


def get_error_distribution(points):
    all_errors = []
    for track in points.values():
        all_errors += track.reprojection_errors.values()
    robust_mean = np.median(all_errors, axis=0)
    robust_std = 1.486 * \
        np.median(np.linalg.norm(all_errors - robust_mean, axis=1))
    return robust_mean, robust_std


def get_actual_threshold(config, points):
    filter_type = config['bundle_outlier_filtering_type']
    if filter_type == 'FIXED':
        return config['bundle_outlier_fixed_threshold']
    elif filter_type == 'AUTO':
        mean, std = get_error_distribution(points)
        return config['bundle_outlier_auto_ratio'] * np.linalg.norm(mean + std)
    else:
        return 1.0


def remove_outliers(graph, reconstruction, config, points=None):
    """Remove points with large reprojection error.

    A list of point ids to be processed can be given in ``points``.
    """
    if points is None:
        points = reconstruction.points
    threshold_sqr = get_actual_threshold(config, reconstruction.points)**2
    outliers = []
    for point_id in points:
        for shot_id, error in reconstruction.points[point_id].reprojection_errors.items():
            error_sqr = error[0]**2 + error[1]**2
            if error_sqr > threshold_sqr:
                outliers.append((point_id, shot_id))

    for track, shot_id in outliers:
        del reconstruction.points[track].reprojection_errors[shot_id]
        graph.remove_edge(track, shot_id)
    for track, _ in outliers:
        if track not in reconstruction.points:
            continue
        if len(graph[track]) < 2:
            del reconstruction.points[track]
            graph.remove_node(track)
    logger.info("Removed outliers: {}".format(len(outliers)))
    return len(outliers)


def shot_lla_and_compass(shot, reference):
    """Lat, lon, alt and compass of the reconstructed shot position."""
    topo = shot.pose.get_origin()
    lat, lon, alt = reference.to_lla(*topo)

    dz = shot.viewing_direction()
    angle = np.rad2deg(np.arctan2(dz[0], dz[1]))
    angle = (angle + 360) % 360
    return lat, lon, alt, angle


def align_two_reconstruction(r1, r2, common_tracks, threshold):
    """Estimate similarity transform between two reconstructions."""
    t1, t2 = r1.points, r2.points

    if len(common_tracks) > 6:
        p1 = np.array([t1[t[0]].coordinates for t in common_tracks])
        p2 = np.array([t2[t[1]].coordinates for t in common_tracks])

        # 3 samples / 100 trials / 50% outliers = 0.99 probability
        # with probability = 1-(1-(1-outlier)^model)^trial
        T, inliers = multiview.fit_similarity_transform(
            p1, p2, max_iterations=100, threshold=threshold)
        if len(inliers) > 0:
            return True, T, inliers
    return False, None, None


def merge_two_reconstructions(r1, r2, config, threshold=1):
    """Merge two reconstructions with common tracks IDs."""
    common_tracks = list(set(r1.points) & set(r2.points))
    worked, T, inliers = align_two_reconstruction(
        r1, r2, common_tracks, threshold)

    if worked and len(inliers) >= 10:
        s, A, b = multiview.decompose_similarity_transform(T)
        r1p = r1
        apply_similarity(r1p, s, A, b)
        r = r2
        r.shots.update(r1p.shots)
        r.points.update(r1p.points)
        align_reconstruction(r, None, config)
        return [r]
    else:
        return [r1, r2]


def merge_reconstructions(reconstructions, config):
    """Greedily merge reconstructions with common tracks."""
    num_reconstruction = len(reconstructions)
    ids_reconstructions = np.arange(num_reconstruction)
    remaining_reconstruction = ids_reconstructions
    reconstructions_merged = []
    num_merge = 0

    for (i, j) in combinations(ids_reconstructions, 2):
        if (i in remaining_reconstruction) and (j in remaining_reconstruction):
            r = merge_two_reconstructions(
                reconstructions[i], reconstructions[j], config)
            if len(r) == 1:
                remaining_reconstruction = list(set(
                    remaining_reconstruction) - set([i, j]))
                for k in remaining_reconstruction:
                    rr = merge_two_reconstructions(r[0], reconstructions[k],
                                                   config)
                    if len(r) == 2:
                        break
                    else:
                        r = rr
                        remaining_reconstruction = list(set(
                            remaining_reconstruction) - set([k]))
                reconstructions_merged.append(r[0])
                num_merge += 1

    for k in remaining_reconstruction:
        reconstructions_merged.append(reconstructions[k])

    logger.info("Merged {0} reconstructions".format(num_merge))

    return reconstructions_merged


def paint_reconstruction(data, tracks_manager, reconstruction):
    """Set the color of the points from the color of the tracks."""
    for k, point in reconstruction.points.items():
        point.color = map(float, next(
            iter(tracks_manager.get_track_observations(k).values())).color)


class ShouldBundle:
    """Helper to keep track of when to run bundle."""

    def __init__(self, data, reconstruction):
        self.interval = data.config['bundle_interval']
        self.new_points_ratio = data.config['bundle_new_points_ratio']
        self.reconstruction = reconstruction
        self.done()

    def should(self):
        max_points = self.num_points_last * self.new_points_ratio
        max_shots = self.num_shots_last + self.interval
        return (len(self.reconstruction.points) >= max_points or
                len(self.reconstruction.shots) >= max_shots)

    def done(self):
        self.num_points_last = len(self.reconstruction.points)
        self.num_shots_last = len(self.reconstruction.shots)


class ShouldRetriangulate:
    """Helper to keep track of when to re-triangulate."""

    def __init__(self, data, reconstruction):
        self.active = data.config['retriangulation']
        self.ratio = data.config['retriangulation_ratio']
        self.reconstruction = reconstruction
        self.done()

    def should(self):
        max_points = self.num_points_last * self.ratio
        return self.active and len(self.reconstruction.points) > max_points

    def done(self):
        self.num_points_last = len(self.reconstruction.points)


def grow_reconstruction(data, tracks_manager, graph_inliers, reconstruction, images, camera_priors, gcp):
    """Incrementally add shots to an initial reconstruction."""
    config = data.config
    report = {'steps': []}

    align_reconstruction(reconstruction, gcp, config)
    bundle(graph_inliers, reconstruction, camera_priors, None, config)
    remove_outliers(graph_inliers, reconstruction, config)

    should_bundle = ShouldBundle(data, reconstruction)
    should_retriangulate = ShouldRetriangulate(data, reconstruction)
    while True:
        if config['save_partial_reconstructions']:
            paint_reconstruction(data, tracks_manager, reconstruction)
            data.save_reconstruction(
                [reconstruction], 'reconstruction.{}.json'.format(
                    datetime.datetime.now().isoformat().replace(':', '_')))

        candidates = reconstructed_points_for_images(
            tracks_manager, reconstruction, images)
        if not candidates:
            break

        logger.info("-------------------------------------------------------")
        threshold = data.config['resection_threshold']
        min_inliers = data.config['resection_min_inliers']
        for image, num_tracks in candidates:

            camera = reconstruction.cameras[data.load_exif(image)['camera']]
            metadata = get_image_metadata(data, image)
            ok, resrep = resect(tracks_manager, graph_inliers, reconstruction, image,
                                camera, metadata, threshold, min_inliers)
            if not ok:
                continue

            bundle_single_view(graph_inliers, reconstruction, image,
                               camera_priors, data.config)

            logger.info("Adding {0} to the reconstruction".format(image))
            step = {
                'image': image,
                'resection': resrep,
                'memory_usage': current_memory_usage()
            }
            report['steps'].append(step)
            images.remove(image)

            np_before = len(reconstruction.points)
            triangulate_shot_features(
                tracks_manager, graph_inliers, reconstruction, image, config)
            np_after = len(reconstruction.points)
            step['triangulated_points'] = np_after - np_before

            if should_retriangulate.should():
                logger.info("Re-triangulating")
                align_reconstruction(reconstruction, gcp, config)
                b1rep = bundle(graph_inliers, reconstruction, camera_priors,
                               None, config)
                rrep = retriangulate(
                    tracks_manager, graph_inliers, reconstruction, config)
                b2rep = bundle(graph_inliers, reconstruction, camera_priors,
                               None, config)
                remove_outliers(graph_inliers, reconstruction, config)
                step['bundle'] = b1rep
                step['retriangulation'] = rrep
                step['bundle_after_retriangulation'] = b2rep
                should_retriangulate.done()
                should_bundle.done()
            elif should_bundle.should():
                align_reconstruction(reconstruction, gcp, config)
                brep = bundle(graph_inliers, reconstruction, camera_priors,
                              None, config)
                remove_outliers(graph_inliers, reconstruction, config)
                step['bundle'] = brep
                should_bundle.done()
            elif config['local_bundle_radius'] > 0:
                bundled_points, brep = bundle_local(
                    graph_inliers, reconstruction, camera_priors, None, image, config)
                remove_outliers(
                    graph_inliers, reconstruction, config, bundled_points)
                step['local_bundle'] = brep

            break
        else:
            logger.info("Some images can not be added")
            break

    logger.info("-------------------------------------------------------")

    align_reconstruction(reconstruction, gcp, config)
    bundle(graph_inliers, reconstruction, camera_priors, gcp, config)
    remove_outliers(graph_inliers, reconstruction, config)

    paint_reconstruction(data, tracks_manager, reconstruction)
    return reconstruction, report


def _length_histogram(points, graph):
    hist = defaultdict(int)
    for p in points:
        hist[len(graph[p])] += 1
    return np.array(list(hist.keys())), np.array(list(hist.values()))


def compute_statistics(reconstruction, graph):
    stats = {}
    stats['points_count'] = len(reconstruction.points)
    stats['cameras_count'] = len(reconstruction.shots)

    hist, values = _length_histogram(reconstruction.points, graph)
    stats['observations_count'] = int(sum(hist * values))
    if len(reconstruction.points) > 0:
        stats['average_track_length'] = float(
            stats['observations_count']) / len(reconstruction.points)
    else:
        stats['average_track_length'] = -1
    tracks_notwo = sum(
        [1 if len(graph[p]) > 2 else 0 for p in reconstruction.points])
    if tracks_notwo > 0:
        stats['average_track_length_notwo'] = float(
            sum(hist[1:] * values[1:])) / tracks_notwo
    else:
        stats['average_track_length_notwo'] = -1
    return stats


def incremental_reconstruction(data, tracks_manager):
    """Run the entire incremental reconstruction pipeline."""
    logger.info("Starting incremental reconstruction")
    report = {}
    chrono = Chronometer()

    images = tracks_manager.get_shot_ids()

    if not data.reference_lla_exists():
        data.invent_reference_lla(images)

    remaining_images = set(images)
    camera_priors = data.load_camera_models()
    gcp = data.load_ground_control_points()
    common_tracks = tracking.all_common_tracks(tracks_manager)
    reconstruction = pymap.Map()
    for cam_id, (cam_name, c) in enumerate(camera_priors.items()):
        # Create the cameras
        cam = pymap.PerspectiveCamera(
            c.width, c.height, c.projection_type,
            c.focal, c.k1, c.k2)
        # Create the shot cameras
        cam_model = reconstruction.create_cam_model(cam_name, cam)
        reconstruction.create_shot_camera(cam_id, cam_model, cam_name)


    pairs = compute_image_pairs(common_tracks, camera_priors, data)
    chrono.lap('compute_image_pairs')
    report['num_candidate_image_pairs'] = len(pairs)
    report['reconstructions'] = []
    for im1, im2 in pairs:
        if im1 in remaining_images and im2 in remaining_images:
            rec_report = {}
            report['reconstructions'].append(rec_report)
            _, p1, p2 = common_tracks[im1, im2]
            # create the shots!
            success, rec_report['bootstrap'] = bootstrap_reconstruction(
                data, tracks_manager, reconstruction, camera_priors, im1, im2, p1, p2)

            if success:
                remaining_images.remove(im1)
                remaining_images.remove(im2)
                reconstruction, rec_report['grow'] = grow_reconstruction(
                    data, tracks_manager, graph_inliers, reconstruction, remaining_images, camera_priors, gcp)
                reconstructions.append(reconstruction)
                reconstructions = sorted(reconstructions,
                                         key=lambda x: -len(x.shots))
                rec_report['stats'] = compute_statistics(
                    reconstruction, graph_inliers)
                logger.info(rec_report['stats'])

    for k, r in enumerate(reconstructions):
        logger.info("Reconstruction {}: {} images, {} points".format(
            k, len(r.shots), len(r.points)))
    logger.info("{} partial reconstructions in total.".format(
        len(reconstructions)))
    chrono.lap('compute_reconstructions')
    report['wall_times'] = dict(chrono.lap_times())
    report['not_reconstructed_images'] = list(remaining_images)
    return report, reconstructions


class Chronometer:
    def __init__(self):
        self.start()

    def start(self):
        t = timer()
        lap = ('start', 0, t)
        self.laps = [lap]
        self.laps_dict = {'start': lap}

    def lap(self, key):
        t = timer()
        dt = t - self.laps[-1][2]
        lap = (key, dt, t)
        self.laps.append(lap)
        self.laps_dict[key] = lap

    def lap_time(self, key):
        return self.laps_dict[key][1]

    def lap_times(self):
        return [(k, dt) for k, dt, t in self.laps[1:]]

    def total_time(self):
        return self.laps[-1][2] - self.laps[0][2]
