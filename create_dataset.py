# -*- coding: utf-8 -*-
import numpy as np
import os
from render import get_smpl_mesh, render_scene, save_render
from utils import disjoint_indices
from camera import get_sphere_poses, get_pose_matrix, get_circle_poses, get_circle_on_sphere_poses
import pickle
from skimage.color import gray2rgb
import configargparse


np.random.seed(0)

def config_parser():
    """
    Configuration parser for training.

    """
    parser = configargparse.ArgumentParser()
    parser.add_argument('--save_dir', default="data", help='save directory for dataset')
    parser.add_argument('--resolution', default=128, type=int, help='height and width of renders')
    parser.add_argument('--camera_radius', default=2.4, type=float, help='radius of sphere on which camera moves')
    parser.add_argument('--dataset_type', default="nerf", type=str, help='choose dataset type for model [nerf, pix2pix]')
    parser.add_argument('--train_val_ratio', default=0.8, type=float, help='train validation ratio')
    parser.add_argument('--start_angle', default=-90, type=int, help='Start angle for phi and theta on sphere')
    parser.add_argument('--end_angle', default=90, type=int, help='End angle for phi and theta on sphere')
    parser.add_argument('--number_steps', default=10, type=int, help='Number of angles inbetween start and end angle')
    parser.add_argument('--camera_path', default="sphere", help='Geometric object along which the camera is moved [sphere, circle]')
    return parser

def save_split(save_dir, camera_transform, indices, split,
               height, width, camera_angle_x, mesh, far, dataset_type, human_poses=None):
    if dataset_type not in ["nerf", "pix2pix", "smpl_nerf"]:
        raise Exception("This dataset type is unknown")
    directory = os.path.join(save_dir, split)
    if not os.path.exists(directory):
        os.makedirs(directory)
    camera_transform = camera_transform[indices]
    
    image_names = ["img_{:03d}.png".format(index) for index in indices]
    print("Length of {} set: {}".format(split, len(image_names)))
    image_transform_map = {image_name: camera_pose
                           for (image_name, camera_pose) in zip(image_names, camera_transform)}
    dict = {'camera_angle_x': camera_angle_x, 
            'image_transform_map': image_transform_map,
            }
    for image_name, camera_pose in image_transform_map.items():
        if dataset_type == "nerf":
            img = render_scene(mesh, camera_pose, get_pose_matrix(), camera_pose,
                           height, width, camera_angle_x)
        if dataset_type == "pix2pix":
            rgb, depth = render_scene(mesh, camera_pose, get_pose_matrix(), camera_pose,
                           height, width, camera_angle_x, return_depth=True)

            depth = (depth / far * 255).astype(np.uint8)
            img = np.concatenate([rgb, gray2rgb(depth)], 1)
        save_render(img, os.path.join(directory, image_name))
    print("Saved {} images under: {}".format(split, directory))
    pkl_file_name = os.path.join(directory, 'transforms.pkl')
    with open(pkl_file_name, 'wb') as handle:
        pickle.dump(dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
    print("Saved {} images to transforms map under: {} \n".format(split, pkl_file_name))


def create_dataset():
    parser = config_parser()
    args = parser.parse_args()
    camera_angle_x = np.pi / 3
    if args.camera_path == "sphere":
        dataset_size = args.number_steps ** 2
    elif args.camera_path == "circle":
        dataset_size = args.number_steps
    elif args.camera_path == "circle_on_sphere":
        dataset_size = args.number_steps
    else:
        raise Exception("This camera path is unknown")
    print("Dataset size: ",dataset_size)
    far = args.camera_radius * 2 # For depth normalization
    
    smpl_file_name = "SMPLs/smpl/models/basicModel_f_lbs_10_207_0_v1.0.0.pkl"
    texture_file_name = 'textures/texture.jpg'
    uv_map_file_name = 'textures/smpl_uv_map.npy'
    mesh = get_smpl_mesh(smpl_file_name, texture_file_name, uv_map_file_name)
    
    if args.camera_path == "sphere":
        camera_transform, camera_angles = get_sphere_poses(args.start_angle, args.end_angle, args.number_steps,
                                    args.camera_radius)
    elif args.camera_path == "circle":
        camera_transform, camera_angles = get_circle_poses(args.start_angle, args.end_angle, args.number_steps,
                                    args.camera_radius)
    elif args.camera_path == "circle_on_sphere":
        camera_transform, camera_angles = get_circle_on_sphere_poses(args.number_steps,20,
                                    args.camera_radius)
    train_indices, val_indices = disjoint_indices(dataset_size, args.train_val_ratio)
    train_indices, val_indices = sorted(train_indices), sorted(val_indices)
    save_split(args.save_dir, camera_transform, train_indices, "train",
               args.resolution, args.resolution, camera_angle_x, mesh, far,
               args.dataset_type)
    save_split(args.save_dir, camera_transform, val_indices, "val",
               args.resolution, args.resolution, camera_angle_x, mesh, far,
               args.dataset_type)
    
if __name__ == "__main__":
    create_dataset()
