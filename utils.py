import pickle

import matplotlib.pyplot as plt
import cv2
import numpy as np
import torch
import trimesh
from torch.utils.tensorboard import SummaryWriter
import torch.distributions as D
from torch.distributions import MixtureSameFamily

from torchsearchsorted import searchsorted

from typing import Tuple
import os

from trimesh.ray.ray_triangle import RayMeshIntersector
from scipy.spatial.transform import Rotation as R
from mpl_toolkits.axes_grid1 import make_axes_locatable


def get_rays(H: int, W: int, focal: float,
             camera_transform: np.array) -> [np.array, np.array]:
    """
    Returns direction and translations of camera rays going through image
    plane.

    Parameters
    ----------
    H : int
        Height of image.
    W : int
        Width of image.
    focal : float
        Focal lenght of camera.
    camera_transform : np.array (4, 4)
        Camera transformation matrix.

    Returns
    -------
    rays_translation : np.array (H, W, 3)
        Translational vector of camera transform dublicated HxW times.
    rays_direction : np.array (H, W, 3)
        Directions of rays going through camera plane.
    """
    i, j = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32), indexing='xy')
    dirs = np.stack([(i - W * .5) / focal, -(j - H * .5) / focal, -np.ones_like(i)], -1)
    rays_direction = np.sum(dirs[..., np.newaxis, :] * camera_transform[:3, :3], -1)  # dirs @ camera_transform
    rays_translation = np.broadcast_to(camera_transform[:3, -1], np.shape(rays_direction))
    return rays_translation, rays_direction


class GaussianMixture():
    def __init__(self, means: np.ndarray, std, device):
        """
        Create a gaussian mixture model with means and the same diagonal std for every gaussian

        Parameters
        ---------
        means: [num_gaussians, dim_gaussian]
        std: float
        """

        self.means = torch.from_numpy(means).to(device)
        self.var = std ** 2
        cov_det = self.var ** means.shape[-1]
        self.factor = 1 / np.sqrt(((2 * np.pi) ** means.shape[-1] * cov_det))

    def pdf(self, samples):
        """
        returns the probability density for each sample

        Parameters
        ----------
        samples : torch.Tensor ([batchsize, number_samples, dim_gaussian])
            samples for which to compute the density

        Returns
        -------
        mixture_probs: torch.Tensor ([number_samples])
            Probability density of each sample under the gaussian mixture
        """
        if samples.shape[-1] != self.means.shape[-1]:
            raise ValueError("Dimension of samples is ", samples.shape[-1], " while dimension of gaussians is ",
                             self.means.shape[-1])
        mu = self.means[None, None, :, :].repeat(
            (samples.shape[0], samples.shape[1], 1, 1))  # [batchsize, num_samples, num_gaussians, dim_gaussian]
        samples_minus_mu = samples[..., None, :] - mu  # [num_samples, num_gaussians, dim_gaussian]
        gaussians_probs = self.factor * torch.exp(
            -0.5 * torch.sum(samples_minus_mu ** 2, dim=-1) / self.var)  # [num_samples, num_gaussians]
        mixture_probs = torch.sum(gaussians_probs, dim=-1) / gaussians_probs.shape[-1]  # [num_samples]
        return mixture_probs


class PositionalEncoder():
    def __init__(self, number_frequencies, include_identity):
        freq_bands = torch.pow(2, torch.linspace(0., number_frequencies - 1, number_frequencies))
        self.embed_fns = []
        self.output_dim = 0
        self.number_frequencies = number_frequencies
        self.include_identity = include_identity
        if include_identity:
            self.embed_fns.append(lambda x: x)
            self.output_dim += 1

        for freq in freq_bands:
            for periodic_fn in [torch.sin, torch.cos]:
                self.embed_fns.append(lambda x, periodic_fn=periodic_fn, freq=freq: periodic_fn(x * freq))
                self.output_dim += 1

    def encode(self, coordinate):
        return torch.cat([fn(coordinate) for fn in self.embed_fns], -1)


def raw2outputs(raw: torch.Tensor, z_vals: torch.Tensor,
                samples_directions: torch.Tensor, args) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Transforms model's predictions to semantically meaningful values.

    Parameters
    ----------
    raw : torch.Tensor ([batch_size, number_coarse_samples, 4])
        Output from network.
    z_vals : torch.Tensor ([batch_size, number_coarse_samples])
        Depth of samples along ray.
    samples_directions : torch.Tensor ([3])
        Directions of samples.
    sigma_noise_std : float, optional
        Regularization: std of added noise to models prediction for density.
        The default is 0.
    white_background : bool, optional
        If True, assume a white background. The default is False.

    Returns
    -------
    rgb : torch.Tensor ([batch_size, 3])
        Estimated RGB color of rays.
    weights : torch.Tensor ([batch_size, 3])
        Weights assigned to each sampled color.
    """
    raw2density = lambda raw, dists: 1. - torch.exp(-torch.nn.functional.relu(raw) * dists)

    dists = z_vals[..., 1:] - z_vals[..., :-1]
    dists = torch.cat([dists, torch.Tensor([1e10]).expand(dists[..., :1].shape)], -1)  # [batchsize, number_samples]

    dists = dists * torch.norm(samples_directions, dim=-1)

    rgb = torch.sigmoid(raw[..., :3])  # [batchsize, number_samples, 3]

    noise = 0.
    if args.sigma_noise_std > 0.:
        noise = torch.normal(0, args.sigma_noise_std, raw[..., 3].shape)
    density = raw2density(raw[..., 3] + noise, dists)  # [batchsize, number_samples]
    one_minus_density = 1. - density + 1e-10

    # remove last column from one_minus_alhpa and add ones as first column so cumprod gives us the exclusive cumprod like tf.cumprod(exclusive=True)
    ones = torch.ones(one_minus_density.shape[:-1]).unsqueeze(-1)
    exclusive = torch.cat([ones, one_minus_density[..., :-1]], -1)
    weights = density * torch.cumprod(exclusive, -1)
    rgb = torch.sum(weights[..., None] * rgb, -2)  # [batchsize, 3]

    depth_map = torch.sum(weights * z_vals, -1)
    disp_map = 1. / torch.max(torch.full(depth_map.shape, 1e-10), depth_map / torch.sum(weights, -1))
    acc_map = torch.sum(weights, -1)
    if args.white_background:
        rgb = rgb + (1. - acc_map[..., None])

    return rgb, weights, density


def sample_pdf(bins, weights, number_samples):
    """
    Hierarchical sampling
    """
    # Get pdf
    weights = weights + 1e-5  # prevent nans
    pdf = weights / torch.sum(weights, -1, keepdim=True)
    cdf = torch.cumsum(pdf, -1)
    cdf = torch.cat([torch.zeros_like(cdf[..., :1]), cdf], -1)  # (batch, len(bins))

    # Take uniform samples
    u = torch.linspace(0., 1., steps=number_samples)
    u = u.expand(list(cdf.shape[:-1]) + [number_samples])

    # Invert CDF
    u = u.contiguous()
    inds = searchsorted(cdf, u, side='right')
    below = torch.max(torch.zeros_like(inds - 1), inds - 1)
    above = torch.min(cdf.shape[-1] - 1 * torch.ones_like(inds), inds)
    inds_g = torch.stack([below, above], -1)  # (batch, N_samples, 2)

    # cdf_g = tf.gather(cdf, inds_g, axis=-1, batch_dims=len(inds_g.shape)-2)
    # bins_g = tf.gather(bins, inds_g, axis=-1, batch_dims=len(inds_g.shape)-2)
    matched_shape = [inds_g.shape[0], inds_g.shape[1], cdf.shape[-1]]
    cdf_g = torch.gather(cdf.unsqueeze(1).expand(matched_shape), 2, inds_g)
    bins_g = torch.gather(bins.unsqueeze(1).expand(matched_shape), 2, inds_g)

    denom = (cdf_g[..., 1] - cdf_g[..., 0])
    denom = torch.where(denom < 1e-5, torch.ones_like(denom), denom)
    t = (u - cdf_g[..., 0]) / denom
    samples = bins_g[..., 0] + t * (bins_g[..., 1] - bins_g[..., 0])

    return samples


def fine_sampling(ray_translation: torch.Tensor, samples_directions: torch.Tensor,
                  z_vals: torch.Tensor, weights: torch.Tensor,
                  number_samples: int) -> Tuple[torch.tensor, torch.tensor]:
    """
    Obtain additional samples using weights assigned to colors by the
    coarse net.

    Parameters
    ----------
    ray_translation : torch.Tensor ([batch_size, 3])
        Translation of rays.
    samples_directions : torch.Tensor ([batch_size, 3])
        Directions of samples.
    z_vals : torch.Tensor ([batch_size, number_coarse_samples])
        Depth of coarse samples along ray.
    weights : torch.Tensor ([batch_size, 3])
        Weights assigned to each sampled color.
    number_samples : int
        Number of fine samples.

    Returns
    -------
    z_vals : torch.Tensor ([batch_size, number_coarse_samples])
        Depth of fine samples along ray.
    ray_samples_fine : torch.Tensor ([batch_size, number_coarse_samples + number_fine_samples])
        Fine samples along ray.
    """
    z_vals_mid = .5 * (z_vals[..., 1:] + z_vals[..., :-1])
    z_samples = sample_pdf(z_vals_mid, weights[..., 1:-1], number_samples)
    z_samples = z_samples.detach()
    z_vals, _ = torch.sort(torch.cat([z_vals, z_samples], -1), -1)
    ray_samples_fine = ray_translation[..., None, :] + samples_directions[..., None, :] * z_vals[..., :,
                                                                                          None]  # [batchsize, number_coarse_samples + number_fine_samples, 3]
    return z_vals, ray_samples_fine


def save_run(file_location: str, model_coarse, model_fine, dataset, solver,
             parser):
    """
    Save coarse and fine model and training configuration
    """
    args = parser.parse_args()
    run = {'model_coarse': model_coarse,
           'model_fine': model_fine,
           'position_encoder': {'number_frequencies': solver.positions_encoder.number_frequencies,
                                'include_identity': solver.positions_encoder.include_identity},
           'direction_encoder': {'number_frequencies': solver.directions_encoder.number_frequencies,
                                 'include_identity': solver.directions_encoder.include_identity},
           'dataset_transform': dataset.transform,
           'white_background': args.white_background,
           'number_fine_samples': args.number_fine_samples,
           'height': dataset.h,
           'width': dataset.w,
           'focal': dataset.focal}
    with open(file_location, 'wb') as file:
        pickle.dump(run, file, protocol=pickle.HIGHEST_PROTOCOL)

    parser.write_config_file(args, [os.path.join(os.path.dirname(file_location), 'config.txt')])


def disjoint_indices(size: int, ratio: float, random=True) -> Tuple[np.ndarray, np.ndarray]:
    """
        Creates disjoint set of indices where all indices together are size many indices. The first set of the returned
        tuple has size*ratio many indices and the second one has size*(ratio-1) many indices.

        Args:
            size (int): total number of indices returned. First and second array together
            ratio (float): relative sizes between the returned index arrays
            random (boolean): should the indices be randomly sampled
    """
    if random:
        train_indices = np.random.choice(np.arange(size), int(size * ratio), replace=False)
        val_indices = np.setdiff1d(np.arange(size), train_indices, assume_unique=True)
        return train_indices, val_indices

    indices = np.arange(size)
    split_index = int(size * ratio)
    return indices[:split_index], indices[split_index:]


def get_dependent_rays_indices(ray_translation: np.array, ray_direction: np.array,
                               canonical: trimesh.base.Trimesh, goal: trimesh.base.Trimesh,
                               camera_transform: np.array, h: int, w: int, f: float) -> np.array:
    """
    Takes one ray (with translation + direction) and returns all dependent
    rays (as camera pixels) and an empty list if there is no dependent ray.


    Parameters
    ----------
    ray_translation : np.array
        Point on orgin of ray.
    ray_direction : np.array
        Direction of ray.
    canonical : trimesh.base.Trimesh
        Trimesh of SMPL in canonical pose.
    goal : trimesh.base.Trimesh
        Trimesh of SMPL in goal pose.
    camera_transform : np.array
        World to Camera transformation.
    h : int
        Height of image.
    w : int
        Width of image.
    f : float
        Focal length of camera.

    Returns
    -------
    list(np.array)
        Camera pixels of dependent rays.

    """

    intersector = RayMeshIntersector(canonical)
    intersections = intersector.intersects_location([ray_translation], [ray_direction])
    intersections_points = intersections[0]  # (N_intersects, 3)
    intersections_face_indices = intersections[2]  # (N_intersects, )
    if len(intersections_face_indices) == 0:
        return []  # Return  empty list if there are no dependent rays

    goal_intersections = []
    vertices = []
    for i, face_idx in enumerate(intersections_face_indices):
        vertex_indices = canonical.faces[face_idx]
        canonical_vertices = canonical.vertices[vertex_indices]
        goal_vertices = goal.vertices[vertex_indices]
        lin_coeffs_vertices = np.linalg.solve(canonical_vertices.T, intersections_points[i])
        goal_intersection = goal_vertices.T.dot(lin_coeffs_vertices)
        goal_intersections.append(goal_intersection)
        vertices.append(vertex_indices)  # For painting human
    goal_intersections = np.array(goal_intersections)
    rot_1 = R.from_euler('xyz', [0, 180, 0], degrees=True).as_matrix()
    rot_2 = R.from_euler('xyz', [0, 0, 180], degrees=True).as_matrix()
    goal_intersections = goal_intersections - camera_transform[:3,
                                              3]  # This translates the intersections  --> Now the intersections are in the camera frame
    world2camera = rot_2.dot(rot_1.dot(camera_transform[:3, :3].T))  # rot_2 after rot_1 after camera_transform
    goal_intersections = np.dot(world2camera,
                                goal_intersections.T).T  # This rotates the intersections with the camera rotation matrix

    rvec, tvec = np.zeros(3), np.zeros(3)  # Now no further trafo is needed
    camera_matrix = np.array([[f, 0.0, w / 2],
                              [0.0, f, h / 2],
                              [0.0, 0.0, 1.0]])
    distortion_coeffs = np.array([0.0, 0.0, 0.0, 0.0])
    camera_coords = cv2.projectPoints(goal_intersections, rvec, tvec, camera_matrix, distortion_coeffs)[0]
    return np.round(camera_coords.reshape(-1, 2)), vertices


def tensorboard_rerenders(writer: SummaryWriter, number_validation_images, rerender_images, ground_truth_images, step,
                          warps=None):
    if number_validation_images > len(rerender_images):
        print('there are only ', len(rerender_images),
              ' in the validation directory which is less than the specified number_validation_images: ',
              number_validation_images, ' So instead ', len(rerender_images),
              ' images are sent to tensorboard')
        number_validation_images = len(rerender_images)
    else:
        rerender_images = rerender_images[:number_validation_images]

    if number_validation_images > 0:

        if warps is not None:
            image_col = 3
            warps = np.linalg.norm(warps, axis=-1)
            warps = warps.mean(axis=3)
        else:
            image_col = 2
        fig, axarr = plt.subplots(number_validation_images, image_col, sharex=True, sharey=True)

        if len(axarr.shape) == 1:
            axarr = axarr[None, :]
        for i in range(number_validation_images):
            # strange indices after image because matplotlib wants bgr instead of rgb
            axarr[i, 0].imshow(ground_truth_images[i][:, :, ::-1])
            axarr[i, 0].axis('off')
            axarr[i, 1].imshow(rerender_images[i][:, :, ::-1])
            axarr[i, 1].axis('off')
            if warps is not None:
                w = axarr[i, 2].imshow(warps[i])
                axarr[i, 2].axis('off')

                last_axes = plt.gca()
                ax = w.axes
                fig = ax.figure
                divider = make_axes_locatable(ax)
                cax = divider.append_axes("right", size="5%", pad=0.05)
                fig.colorbar(w, cax=cax)
                plt.sca(last_axes)

        axarr[0, 0].set_title('Ground Truth')
        axarr[0, 1].set_title('Rerender')
        if warps is not None:
            axarr[0, 2].set_title('Warp Intensity')

        fig.set_dpi(300)
        writer.add_figure(str(step) + ' validation images', fig, step)
        plt.close()


def tensorboard_warps(writer: SummaryWriter, number_validation_images, samples, warps, step):
    if number_validation_images <= len(samples):
        samples = samples[:number_validation_images]
        warps = warps[:number_validation_images]

    magnitude = np.sum(warps, axis=-1)
    cmap = plt.cm.get_cmap('viridis')
    rgb = cmap(magnitude)[:, :, :3] * 255

    writer.add_mesh('warp', vertices=samples, colors=rgb, global_step=step)


def tensorboard_densities(writer: SummaryWriter, number_validation_images, samples, densities, step):
    if number_validation_images <= len(samples):
        samples = samples[:number_validation_images]
        densities = densities[:number_validation_images]

    # map all samples with a low density to origin
    # samples = np.where(densities[:,:,None] > 0.0001, samples, np.zeros_like(samples))

    cmap = plt.cm.get_cmap('viridis')
    rgb = cmap(densities)[:, :, :3] * 255
    writer.add_mesh('density', vertices=torch.from_numpy(samples), colors=rgb, global_step=step)