import math

import matplotlib

matplotlib.use('Agg')
from matplotlib import pyplot as plt

import argparse
import os
import numpy as np
import torch
import tqdm
import imageio
from torch.autograd import Variable

from kaolin.graphics import NeuralMeshRenderer as Renderer
from kaolin.graphics.nmr.util import get_points_from_angles
from kaolin.rep import TriangleMesh

from models.dummy_smpl_estimator_model import DummySmplEstimatorModel
from util_nmr import normalize_vertices, pre_normalize_vertices

import smplx
from PIL import Image
from io import BytesIO
import torch
import smplx

from util.smpl_sequence_loading import load_pose_sequence

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))


def parse_arguments():
    parser = argparse.ArgumentParser(description='Neural SMPL Mesh Renderer')

    parser.add_argument('--mesh', type=str, default=os.path.join(ROOT_DIR, 'rocket.obj'),
                        help='Path to the mesh OBJ file')
    parser.add_argument('--experiment_name', type=str, default="baseline",
                        help='Experiment name')
    parser.add_argument('--save_path', type=str, default="results/Walk B17 - Walk 2 hop 2 walk_poses",
                        help='Path to the output directory')
    parser.add_argument('--camera_distance', type=float, default=2.4,
                        help='Distance from camera to object center')
    parser.add_argument('--elevation', type=float, default=0,
                        help='Camera elevation')
    parser.add_argument('--texture_size', type=int, default=2,
                        help='Dimension of texture')
    parser.add_argument('--specific_angles_only', type=int, default=1,
                        help='Optimize only specific angles of the pose')
    parser.add_argument('--perturb_betas', type=int, default=0,
                        help='Perturb betas')
    parser.add_argument('--gaussian_blur', type=int, default=0,
                        help='Blur images')
    parser.add_argument('--kernel_size', type=int, default=15,
                        help='Kernel size for gaussian filter')
    parser.add_argument('--sigma', type=int, default=3,
                        help='Sigma for gaussian filter')
    parser.add_argument('--coarse_to_fine', type=int, default=0,
                        help='Perform coarse to fine optimization')
    parser.add_argument('--coarse_to_fine_steps', type=int, default=2,
                        help='Coarse to fine steps')
    parser.add_argument('--iterations', type=int, default=200,
                        help='Optimization iterations')
    parser.add_argument('--image_size', type=int, default=256,
                        help='Image size')
    parser.add_argument('--sequence_file', type=str, default='SMPLs/SMPL_sequences/Walk B17 - Walk 2 hop 2 walk_poses.npz',
                        help='Path to .npz  sequence')
    parser.add_argument('--angle_prior', type=int, default=0,
                        help='SMPLifyAnglePrior')
    
    return parser.parse_args()

def get_gaussian_filter(kernel_size, sigma, channels=3):
    # Create a x, y coordinate grid of shape (kernel_size, kernel_size, 2)
    x_cord = torch.arange(kernel_size)
    x_grid = x_cord.repeat(kernel_size).view(kernel_size, kernel_size)
    y_grid = x_grid.t()
    xy_grid = torch.stack([x_grid, y_grid], dim=-1)
    mean = (kernel_size - 1) / 2.
    variance = sigma ** 2.
    gaussian_kernel = (1. / (2. * math.pi * variance)) * \
                      torch.exp(
                          -torch.sum((xy_grid - mean) ** 2., dim=-1) / \
                          (2 * variance)
                      )
    gaussian_kernel = gaussian_kernel / torch.sum(gaussian_kernel)
    gaussian_kernel = gaussian_kernel.view(1, 1, kernel_size, kernel_size)
    gaussian_kernel = gaussian_kernel.repeat(channels, 1, 1, 1)

    gaussian_filter = torch.nn.Conv2d(in_channels=channels, out_channels=channels,
                                      kernel_size=kernel_size, groups=channels, bias=False)
    gaussian_filter.weight.data = gaussian_kernel
    gaussian_filter.weight.requires_grad = False
    return gaussian_filter

def render_image(gaussian_blur, gaussian_filter, args, vertices, faces,
                 textures, smpl_output, image_size=256):
    
    renderer = Renderer(camera_mode='look_at')
    azimuth = 180
    renderer.eye = get_points_from_angles(
        args.camera_distance, args.elevation, azimuth)
    images, _, _ = renderer(vertices, faces, textures)
    true_image = images[0].permute(1, 2, 0)
    if gaussian_blur:
        true_image = gaussian_filter(true_image.unsqueeze(0).permute(0,3,2,1)).permute(0,3,2,1)[0]
    return true_image

def optimize(args, save_path):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    smpl_file_name = "SMPLs/smpl/models/basicModel_f_lbs_10_207_0_v1.0.0.pkl"
    uv_map_file_name = "textures/smpl_uv_map.npy"
    uv = np.load(uv_map_file_name)
    texture_file_name = "textures/texture.jpg"
    with open(texture_file_name, 'rb') as file:
        texture = Image.open(BytesIO(file.read()))
    model = smplx.create(smpl_file_name, model_type='smpl')
    model = model.to(device)

    gaussian_filter = get_gaussian_filter(args.kernel_size, args.sigma)
    gaussian_filter = gaussian_filter.to(device)    

    betas = torch.tensor([[-0.3596, -1.0232, -1.7584, -2.0465, 0.3387,
                           -0.8562, 0.8869, 0.5013, 0.5338, -0.0210]]).to(device)
    if args.perturb_betas:
        perturbed_betas = Variable(torch.tensor([[3, -1.0232, 1.8, 2.0465, -0.3387,
                                                  0.9, 0.8869, -0.5013, -1, 2]]).to(device),
                                   requires_grad=True)
    else:
        perturbed_betas = betas
    expression = torch.tensor([[2.7228, -1.8139, 0.6270, -0.5565, 0.3251,
                                0.5643, -1.2158, 1.4149, 0.4050, 0.6516]]).to(device)
    perturbed_pose = torch.ones(69).view(1, -1).to(device) * np.deg2rad(4)
    #perturbed_pose[0, 38] = -np.deg2rad(60)
    #perturbed_pose[0, 41] = np.deg2rad(60)

    perturbed_pose = Variable(perturbed_pose, requires_grad=True)
    canonical_pose0 = torch.zeros(2).view(1, -1).to(device)
    canonical_pose1 = torch.zeros(35).view(1, -1).to(device)
    canonical_pose2 = torch.zeros(2).view(1, -1).to(device)
    canonical_pose3 = torch.zeros(27).view(1, -1).to(device)
    arm_angle_l = Variable(torch.tensor([-np.deg2rad(65)]).float().view(1, -1).to(device), requires_grad=True)
    arm_angle_r = Variable(torch.tensor([np.deg2rad(65)]).float().view(1, -1).to(device), requires_grad=True)
    leg_angle_l = Variable(torch.tensor([np.deg2rad(20)]).float().view(1, -1).to(device), requires_grad=True)

    output_true = model(betas=betas, expression=expression,
                             return_verts=True, body_pose=None)

    # Normalize vertices
    # output = model(betas=betas, expression=expression,
    #               return_verts=True, body_pose=perturbed_pose)

    # vertices_goal = output.vertices[0]
    # vertices_abs_max = torch.abs(vertices_goal).max().detach()
    # vertices_min = vertices_goal.min(0)[0][None, :].detach()
    # vertices_max = vertices_goal.max(0)[0][None, :].detach()

    faces = torch.tensor(model.faces * 1.0).to(device)

    mesh_true = TriangleMesh.from_tensors(output_true.vertices[0], faces)
    vertices_true = mesh_true.vertices.unsqueeze(0)
    # vertices = pre_normalize_vertices(mesh.vertices, vertices_min, vertices_max,
    #                                  vertices_abs_max).unsqueeze(0)

    faces = mesh_true.faces.unsqueeze(0)

    textures = torch.ones(
        1, faces.shape[1], args.texture_size, args.texture_size, args.texture_size,
        3, dtype=torch.float32,
        device='cuda'
    )
    renderer_full = Renderer(camera_mode='look_at', image_size=args.image_size)
    azimuth = 180
    renderer_full.eye = get_points_from_angles(
        args.camera_distance, args.elevation, azimuth)
    images, _, _ = renderer_full(vertices_true, faces, textures)
    true_image = images[0].permute(1, 2, 0)
    if args.gaussian_blur:
        true_image = gaussian_filter(true_image.unsqueeze(0).permute(0,3,2,1)).permute(0,3,2,1)[0]
    true_image = true_image.detach()
    imageio.imwrite(save_path+"/true_image.png", 
                    (255 * true_image.detach().cpu().numpy()).astype(np.uint8))

    if args.specific_angles_only and args.perturb_betas:
        optim = torch.optim.Adam([arm_angle_l, arm_angle_r, leg_angle_l, perturbed_betas], lr=1e-2)
    elif args.specific_angles_only:
        optim = torch.optim.Adam([arm_angle_l, arm_angle_r, leg_angle_l], lr=1e-2)
    elif args.perturb_betas:
        optim = torch.optim.Adam([perturbed_pose, perturbed_betas], lr=1e-2)
    else:
        optim = torch.optim.Adam([perturbed_pose], lr=1e-2)
    results = []
    arm_parameters_l = []
    arm_parameters_r = []
    beta_diffs = []
    losses = []
    image_size = args.image_size
    if args.coarse_to_fine:
        image_size = int(image_size/2**args.coarse_to_fine_steps)
    renderer = renderer_full
    for i in range(args.iterations):
        if args.coarse_to_fine and i % int(args.iterations/args.coarse_to_fine_steps) == 0:
            renderer = Renderer(camera_mode='look_at', image_size=image_size)
            azimuth = 180
            renderer.eye = get_points_from_angles(
                args.camera_distance, args.elevation, azimuth)
            images, _, _ = renderer(vertices_true, faces, textures)
            true_image = images[0].permute(1, 2, 0)
            if args.gaussian_blur:
                true_image = gaussian_filter(true_image.unsqueeze(0).permute(0,3,2,1)).permute(0,3,2,1)[0]
            true_image = true_image.detach()
            image_size *= 2
        optim.zero_grad()
        if args.specific_angles_only:
            perturbed_pose = torch.cat(
                [canonical_pose0, leg_angle_l, canonical_pose1, arm_angle_l, canonical_pose2, arm_angle_r,
                 canonical_pose3],
                dim=-1)
        output = model(betas=perturbed_betas, expression=expression,
                       return_verts=True, body_pose=perturbed_pose)

        vertices_goal = output.vertices[0]

        mesh = TriangleMesh.from_tensors(vertices_goal, faces)

        vertices = vertices_goal.unsqueeze(0)
        # vertices = pre_normalize_vertices(mesh.vertices, vertices_min, vertices_max,
        #                              vertices_abs_max).unsqueeze(0)

        images, _, _ = renderer(vertices, faces, textures)
        image = images[0].permute(1, 2, 0)
        if i == 0:
            perturbed_images, _, _ = renderer_full(vertices, faces, textures)
            perturbed_image = perturbed_images[0].permute(1, 2, 0)
            perturbed_image = perturbed_image.detach()
            imageio.imwrite(save_path+"/perturbed_image.png", 
                    (255 * perturbed_image.detach().cpu().numpy()).astype(np.uint8))
        if args.gaussian_blur:
            image = gaussian_filter(image.unsqueeze(0).permute(0, 3, 2, 1)).permute(0, 3, 2, 1)[0]
        loss = (image - true_image).abs().mean()
        loss.backward()
        optim.step()

        results.append((255 * image.detach().cpu().numpy()).astype(np.uint8))
        if args.specific_angles_only:
            arm_parameters_l.append(arm_angle_l.item())
            arm_parameters_r.append(arm_angle_r.item())
        if args.perturb_betas:
            beta_diffs.append((betas - perturbed_betas).abs().mean().item())
        losses.append(loss.item())
        print("Loss: ", loss.item())
    return losses, results, arm_parameters_l, arm_parameters_r, beta_diffs
    
def optimize_sequence(gt_poses, args, save_path):
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    smpl_file_name = "SMPLs/smpl/models/basicModel_f_lbs_10_207_0_v1.0.0.pkl"
    uv_map_file_name = "textures/smpl_uv_map.npy"
    uv = np.load(uv_map_file_name)
    texture_file_name = "textures/texture.jpg"
    with open(texture_file_name, 'rb') as file:
        texture = Image.open(BytesIO(file.read()))
    results = []
    losses = []
    model = smplx.create(smpl_file_name, model_type='smpl')
    model = model.to(device)

    gaussian_filter = get_gaussian_filter(args.kernel_size, args.sigma)
    gaussian_filter = gaussian_filter.to(device)    

    betas = torch.tensor([[-0.3596, -1.0232, -1.7584, -2.0465, 0.3387,
                           -0.8562, 0.8869, 0.5013, 0.5338, -0.0210]]).to(device)
    
    perturbed_pose = torch.zeros(69).view(1, -1).to(device)

    perturbed_pose = Variable(perturbed_pose, requires_grad=True)
    for gt_pose in gt_poses[:3]:
        output_true = model(betas=betas, return_verts=True, body_pose=gt_poses[0])
    
        faces = torch.tensor(model.faces * 1.0).to(device)
    
        mesh_true = TriangleMesh.from_tensors(output_true.vertices[0], faces)
        vertices_true = mesh_true.vertices.unsqueeze(0)
    
        faces = mesh_true.faces.unsqueeze(0)
    
        textures = torch.ones(
            1, faces.shape[1], args.texture_size, args.texture_size, args.texture_size,
            3, dtype=torch.float32,
            device='cuda'
        )
        renderer_full = Renderer(camera_mode='look_at', image_size=args.image_size)
        azimuth = 180
        renderer_full.eye = get_points_from_angles(
            args.camera_distance, args.elevation, azimuth)
        images, _, _ = renderer_full(vertices_true, faces, textures)
        true_image = images[0].permute(1, 2, 0)
        if args.gaussian_blur:
            true_image = gaussian_filter(true_image.unsqueeze(0).permute(0,3,2,1)).permute(0,3,2,1)[0]
        true_image = true_image.detach()
        imageio.imwrite(save_path+"/true_image.png", 
                        (255 * true_image.detach().cpu().numpy()).astype(np.uint8))
    
        optim = torch.optim.Adam([perturbed_pose], lr=1e-2)
        
        image_size = args.image_size
        if args.coarse_to_fine:
            image_size = int(image_size/2**args.coarse_to_fine_steps)
            image_size = args.image_size
            kernel_size = args.kernel_size
        renderer = renderer_full
        for i in range(args.iterations):
            if args.coarse_to_fine and i % int(args.iterations/args.coarse_to_fine_steps) == 0:
                renderer = Renderer(camera_mode='look_at', image_size=image_size)
                azimuth = 180
                renderer.eye = get_points_from_angles(
                    args.camera_distance, args.elevation, azimuth)
                images, _, _ = renderer(vertices_true, faces, textures)
                true_image = images[0].permute(1, 2, 0)
                if args.gaussian_blur:
                    gaussian_filter = get_gaussian_filter(kernel_size, sigma=4)
                    gaussian_filter = gaussian_filter.to(device)
                    true_image = gaussian_filter(true_image.unsqueeze(0).permute(0,3,2,1)).permute(0,3,2,1)[0]
                true_image = true_image.detach()
                image_size = args.image_size #image_size *= 2
                kernel_size = int(kernel_size/2)
                print("kernel size: ", kernel_size)
            optim.zero_grad()
            
            output = model(betas=betas,
                           return_verts=True, body_pose=perturbed_pose)
    
            vertices_goal = output.vertices[0]
    
            mesh = TriangleMesh.from_tensors(vertices_goal, faces)
    
            vertices = vertices_goal.unsqueeze(0)
            # vertices = pre_normalize_vertices(mesh.vertices, vertices_min, vertices_max,
            #                              vertices_abs_max).unsqueeze(0)
    
            images, _, _ = renderer(vertices, faces, textures)
            image = images[0].permute(1, 2, 0)
            if i == 0:
                perturbed_images, _, _ = renderer_full(vertices, faces, textures)
                perturbed_image = perturbed_images[0].permute(1, 2, 0)
                perturbed_image = perturbed_image.detach()
                imageio.imwrite(save_path+"/perturbed_image.png", 
                        (255 * perturbed_image.detach().cpu().numpy()).astype(np.uint8))
            if args.gaussian_blur:
                image = gaussian_filter(image.unsqueeze(0).permute(0, 3, 2, 1)).permute(0, 3, 2, 1)[0]
            loss = (image - true_image).abs().mean()**2
            
            # angle prior for elbow and knees
            if args.angle_prior:
                angle_prior_idxs = np.array([52, 55, 9, 12], dtype=np.int64)
                angle_prior_idxs = torch.tensor(angle_prior_idxs, dtype=torch.long).to(device)
                angle_prior_signs = np.array([1, -1, -1, -1])
                                             
                angle_prior_signs = torch.tensor(angle_prior_signs).to(device)
                loss += torch.sum(0.1*torch.exp(perturbed_pose[:, angle_prior_idxs] *
                                 angle_prior_signs).pow(2))
            loss.backward()
            optim.step()
    
            results.append((255 * image.detach().cpu().numpy()).astype(np.uint8))
            losses.append(loss.item())
            print("Loss: ", loss.item())
    return losses, results


def main_single_frame():
    args = parse_arguments()
    save_path = args.save_path + '/' + args.experiment_name
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    torch.autograd.set_detect_anomaly(True)
    
    losses, results, arm_parameters_l, arm_parameters_r, beta_diffs = optimize(args, save_path)
    imageio.mimsave(save_path +"/gif.gif", results, fps=30)
    for idx, image in enumerate(results):
        imageio.imwrite("{}/{:03d}.png".format(save_path, idx), image)
    plt.plot(losses)
    plt.title("applied loss")
    plt.savefig("{}/loss.png".format(save_path))
    plt.clf()
    if args.perturb_betas:
        plt.plot(beta_diffs)
        plt.title("difference in betas")
        plt.savefig("{}/betas.png".format(save_path))
        plt.clf()
    plt.plot(arm_parameters_r)
    plt.title("right arm angle")
    plt.savefig("{}/right.png".format(save_path))
    plt.clf()
    plt.plot(arm_parameters_l)
    plt.title("left arm angle")
    plt.savefig("{}/left.png".format(save_path))
    
def main_sequence():
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    args = parse_arguments()
    experiment_name = "gaussian_blur{}_kernel_size{}_sigma{}_coarse_to_fine{}_\
    coarse_to_fine_steps{}_angle_prior{}".format(args.gaussian_blur, args.kernel_size, args.sigma, 
    args.coarse_to_fine,
        args.coarse_to_fine_steps, args.angle_prior)
    save_path = args.save_path + '/' + experiment_name
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    gt_poses = load_pose_sequence(args.sequence_file, device)
    torch.autograd.set_detect_anomaly(True)
    
    losses, results = optimize_sequence(gt_poses, args, save_path)
    imageio.mimsave(save_path +"/gif.gif", results, fps=30)
    for idx, image in enumerate(results[::5]):
        imageio.imwrite("{}/{:03d}.png".format(save_path, idx), image)
    plt.plot(losses)
    plt.title("applied loss")
    plt.savefig("{}/loss.png".format(save_path))
    plt.clf()

if __name__ == '__main__':
    main_sequence()
