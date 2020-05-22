import glob
import os
from typing import Dict

import matplotlib.pyplot as plt
import cv2
import numpy as np
import json

from camera import get_pose_matrix, get_circle_pose
from inference import inference
from render import get_smpl_mesh, render_scene


def l1_val_rerender(run_file, val_folder='data/val', batchsize=128):
    with open(os.path.join(val_folder, 'transforms.json'), 'r') as transforms_file:
        transforms_dict = json.load(transforms_file)
    image_transform_map: Dict = transforms_dict.get('image_transform_map')
    camera_transforms_list = []
    val_images = []
    image_paths = sorted(glob.glob(os.path.join(val_folder, '*.png')))
    if not len(image_paths) == len(image_transform_map):
        raise ValueError('Number of images in image_directory is not the same as number of transforms')
    for image_path in image_paths:
        val_images.append(cv2.imread(image_path))
        camera_transforms_list.append(image_transform_map[os.path.basename(image_path)])
    rerenders = inference(run_file, camera_transforms_list, batch_size=batchsize)
    val_images = np.array(val_images)
    renders = val_images.reshape((len(val_images), -1))
    rerenders = rerenders.reshape((len(rerenders), -1))
    return np.mean(np.abs(renders - rerenders), axis=-1)


def render_vs_rerender(run_file, camera_transforms, height, width, yfov, output_dir, degrees, batchsize=128):
    smpl_file_name = "SMPLs/smpl/models/basicModel_f_lbs_10_207_0_v1.0.0.pkl"
    texture_file_name = 'textures/texture.jpg'
    uv_map_file_name = 'textures/smpl_uv_map.npy'
    rerenders = inference(run_file, camera_transforms, batchsize)
    mesh = get_smpl_mesh(smpl_file_name, texture_file_name, uv_map_file_name)
    renders = []
    for camera_transform in camera_transforms:
        rgb = render_scene(mesh, camera_transform, get_pose_matrix(), camera_transform,
                           height, width, yfov)
        renders.append(rgb)

    for i, theta in enumerate(degrees):
        fig, axarr = plt.subplots(1, 2, sharex=True, sharey=True)
        # strange indices after image because matplotlib wants bgr instead of rgb
        axarr[0].imshow(renders[i])
        axarr[0].axis('off')
        axarr[1].imshow(rerenders[i][:, :, ::-1])
        axarr[1].axis('off')
        axarr[0].set_title('Ground Truth Theta = ' + str(theta))
        axarr[1].set_title('Rerender')
        fig.set_dpi(400)
        plt.savefig(os.path.join(output_dir, 'render_rerender_{:03d}.png'.format(i)))
    return np.array(renders), rerenders


if __name__ == '__main__':
    run_file = 'runs/Apr16_17-24-53_hannes-MS-7721/128_-90_90_100.pkl'
    output_dir = 'results'
    basename = os.path.basename(run_file)
    output_dir = os.path.join(output_dir, os.path.splitext(basename)[0])
    if not os.path.exists(output_dir):  # create directory if it does not already exist
        os.makedirs(output_dir)

    l1_val_diffs = l1_val_rerender(run_file, val_folder='data/val', batchsize=900)
    stat_file = open(os.path.join(output_dir,'statistics.txt'), 'w+')
    stat_file.write('Mean L1 Val Loss: ' + str(np.mean(l1_val_diffs)))

    height, width, yfov = 128, 128, np.pi / 3
    camera_radius = 2.4
    camera_transforms = []
    degrees = np.arange(90, 110, 2)
    for i in degrees:
        camera_pose = get_circle_pose(i, camera_radius)
        camera_transforms.append(camera_pose)

    renders, rerenders = render_vs_rerender(run_file, camera_transforms, height, width, yfov, output_dir, degrees, batchsize=900)
    renders = renders.reshape((len(renders), -1))
    rerenders = rerenders.reshape((len(rerenders), -1))

    l1_diffs = np.mean(np.abs(renders - rerenders), axis=-1)
    print('average L1 distance: ', np.mean(l1_diffs))
    plt.figure()
    plt.plot(degrees, l1_diffs)
    plt.xlabel('Theta [°]')
    plt.ylabel('L1 loss Ground Truth vs Rerender')
    plt.savefig(os.path.join(output_dir, 'degrees_l1_plot.png'))
