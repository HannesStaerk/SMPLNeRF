# -*- coding: utf-8 -*-
from typing import Tuple
import numpy as np
from scipy.spatial.transform import Rotation as R


def get_pose_matrix(x: float=0, y: float=0, z: float=0,
                    phi: float=0,  theta: float=0, psi: float=0) -> np.array:
    """
    Compute pose matrix for given translation/rotation parameters

    Parameters
    ----------
    x : float, optional
        x coordinate. The default is 0.
    y : float, optional
        y coordinate. The default is 0.
    z : float, optional
        z coordinate. The default is 0.
    phi : float, optional
        rotation around x axis in degrees. The default is 0.
    theta : float, optional
        rotation around y axis in degrees. The default is 0.
    psi : float, optional
        rotation around x axis in degree. The default is 0.

    Returns
    -------
    pose : np.array (4, 4)
        pose matrix in homogeneous representation.

    """
    rot = R.from_euler('xyz', [phi, theta, psi], degrees=True).as_matrix()
    trans = np.array([[x, y, z]])
    pose = np.concatenate((np.concatenate((rot, trans.T), axis=1),
                           [[0, 0, 0, 1]]), axis=0)
    return pose


def get_circle_pose(theta: float, r: float) -> np.array:
    """
    Compute pose matrix for angle theta in xz-circle with radius r around
    y-axis (theta = 0 corresponds position (0, 0, r))

    Parameters
    ----------
    theta : float
        rotation around y axis in degrees.
    r : float
        radius of circle.

    Returns
    -------
    pose : np.array (4, 4)
        pose matrix in homogeneous representation.

    """
    z = r*np.cos(np.radians(theta))
    x = r*np.sin(np.radians(theta))
    pose = get_pose_matrix(x=x, z=z, theta=theta)
    return pose


def get_sphere_pose(phi: float, theta: float, r: float) -> np.array:
    """
    Compute pose matrix for angle theta in xz-circle with radius r around
    y-axis and angle phi in yz-circle around x-axis (spherical coordinates)

    Parameters
    ----------
    phi : float
        rotation around x axis in degrees.
    theta : float
        rotation around y axis in degrees.
    r : float
        radius of circle.

    Returns
    -------
    pose : np.array (4, 4)
        pose matrix in homogeneous representation.

    """
    z = r*np.cos(np.radians(phi))*np.cos(np.radians(theta))
    x = r*np.cos(np.radians(phi))*np.sin(np.radians(theta))
    y = r*np.sin(np.radians(phi))
    pose = get_pose_matrix(x=x, y=y, z=z, theta=theta, phi=-phi)
    return pose


def get_sphere_poses(start_angle: float, end_angle: float,
                     number_steps: int, r: float) -> np.array:
    """
    Compute poses on a sphere between start and end angle (for phi, theta)

    Parameters
    ----------
    start_angle : float
        start angle for theta and phi in degrees.
    end_angle : float
        end angle for theta and phi in degrees.
    number_steps : int
        number of steps between start and end angle.
    r : float
        radius of sphere.

    Returns
    -------
    poses : np.array (number_steps ** 2, 4, 4)
        pose matrices in homogeneous representation.

    """
    phis = np.linspace(start_angle, end_angle, number_steps)
    print("Angle stepsize: {:.2f}°".format((end_angle - start_angle)/number_steps))
    thetas = np.linspace(start_angle, end_angle, number_steps)
    angles = np.transpose([np.tile(phis, len(thetas)),
                           np.repeat(thetas, len(phis))])
    poses = [get_sphere_pose(phi, theta, r) for (phi, theta) in angles]
    return np.array(poses), angles


def camera_origin_direction(x: float, y: float, z: float) -> Tuple[float, float]:
    """
    Calculate phi and theta in degrees for a camera to face the origin
    of the coordinate system

    Parameters
    ----------
    x : float
        x coordinate of camera.
    y : float
        y coordinate of camera.
    z : float
        z coordinate of camera.

    Returns
    -------
    Tuple[float, float]
        phi and theta in degrees.

    """
    phi = np.degrees(np.arctan2(y, z))
    theta = np.degrees(np.arctan2(x, z))
    return phi.item(), theta.item()
