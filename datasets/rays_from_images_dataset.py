import glob
import os
import json

import cv2
import numpy as np
from torch.utils.data import Dataset

from utils import get_rays


class RaysFromImagesDataset(Dataset):
    """
    Dataset of rays from a directory of images and an images camera transforms
    mapping file (used for training).
    """

    def __init__(self, image_directory: str, transforms_file: str,
                 transform) -> None:
        """
        Parameters
        ----------
        image_directory : str
            Path to images.
        transforms_file : str
            File path to file containing transformation mappings.
        transform :
            List of callable transforms for preprocessing.
        """
        super().__init__()
        self.transform = transform
        self.rays = []  # list of arrays with ray translation, ray direction and rgb
        print('Start initializing all rays of all images')
        with open(transforms_file, 'r') as transforms_file:
            transforms_dict = json.load(transforms_file)
        camera_angle_x = transforms_dict['camera_angle_x']
        self.image_transform_map = transforms_dict.get('image_transform_map')
        image_paths = sorted(glob.glob(os.path.join(image_directory, '*.png')))
        if not len(image_paths) == len(self.image_transform_map):
            raise ValueError('Number of images in image_directory is not the same as number of transforms')
        for image_path in image_paths:
            camera_transform = np.array(self.image_transform_map[os.path.basename(image_path)])
            image = cv2.imread(image_path)
            self.h, self.w = image.shape[:2]
            self.focal = .5 * self.w / np.tan(.5 * camera_angle_x)
            rays_translation, rays_direction = get_rays(self.h, self.w, self.focal,
                                                        camera_transform)
            trans_dir_rgb_stack = np.stack([rays_translation, rays_direction, image], -2)
            trans_dir_rgb_list = trans_dir_rgb_stack.reshape((-1, 3, 3))
            self.rays.append(trans_dir_rgb_list)
        if self.rays:
            self.rays = np.concatenate(self.rays)
        print('Finish initializing rays')

    def __getitem__(self, index: int):
        """
        Parameters
        ----------
        index : int
            Index of ray.

        Returns
        -------
        ray_samples : torch.Tensor ([number_coarse_samples, 3])
            Coarse samples along the ray between near and far bound.
        samples_translations : torch.Tensor ([3])
            Translation of samples.
        samples_directions : torch.Tensor ([3])
            Direction of samples.
        z_vals : torch.Tensor ([number_coarse_samples])
            Depth of coarse samples along ray.
        rgb : torch.Tensor ([3])
            RGB value corresponding to ray.
        """
        rays_translation, rays_direction, rgb = self.rays[index]
        ray_samples, samples_translations, samples_directions, z_vals, rgb = self.transform(
            (rays_translation, rays_direction, rgb))

        return ray_samples, samples_translations, samples_directions, z_vals, rgb

    def __len__(self) -> int:
        return len(self.rays)
