import os

import torch
from torch.utils.data import Subset
from torchvision.transforms import transforms
from config_parser import config_parser
from datasets.rays_from_images_dataset import RaysFromImagesDataset
from datasets.smpl_dataset import SmplDataset
from datasets.smpl_nerf_dataset import SmplNerfDataset
from datasets.transforms import CoarseSampling, ToTensor, NormalizeRGB
from models.debug_model import DebugModel
from models.render_ray_net import RenderRayNet
from models.warp_field_net import WarpFieldNet
from solver.append_to_nerf_solver import AppendToNerfSolver
from solver.nerf_solver import NerfSolver
import numpy as np

from solver.smpl_nerf_solver import SmplNerfSolver
from solver.smpl_solver import SmplSolver
from utils import PositionalEncoder, save_run

np.random.seed(0)


def train():
    parser = config_parser()
    args = parser.parse_args()
    if args.model_type not in ["nerf", "smpl_nerf", "append_to_nerf", "smpl"]:
        raise Exception("The model type ", args.model_type, " does not exist.")

    transform = transforms.Compose(
        [NormalizeRGB(), CoarseSampling(args.near, args.far, args.number_coarse_samples), ToTensor()])

    train_dir = os.path.join(args.dataset_dir, 'train')
    val_dir = os.path.join(args.dataset_dir, 'val')
    if args.model_type == "nerf":
        train_data = RaysFromImagesDataset(train_dir, os.path.join(train_dir, 'transforms.json'), transform)
        val_data = RaysFromImagesDataset(val_dir, os.path.join(val_dir, 'transforms.json'), transform)
    elif args.model_type == "smpl":
        train_data = SmplDataset(train_dir, os.path.join(train_dir, 'transforms.json'), args, transform=NormalizeRGB())
        val_data = SmplDataset(val_dir, os.path.join(val_dir, 'transforms.json'), args, transform=NormalizeRGB())
    elif args.model_type == "smpl_nerf" or args.model_type == "append_to_nerf":
        train_data = SmplNerfDataset(train_dir, os.path.join(train_dir, 'transforms.json'), transform)
        val_data = SmplNerfDataset(val_dir, os.path.join(val_dir, 'transforms.json'), transform)

    train_loader = torch.utils.data.DataLoader(train_data, batch_size=args.batchsize, shuffle=True, num_workers=0)
    val_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batchsize_val, shuffle=False, num_workers=0)
    position_encoder = PositionalEncoder(args.number_frequencies_postitional, args.use_identity_positional)
    direction_encoder = PositionalEncoder(args.number_frequencies_directional, args.use_identity_directional)
    model_coarse = RenderRayNet(args.netdepth, args.netwidth, position_encoder.output_dim * 3,
                                direction_encoder.output_dim * 3, skips=args.skips)
    model_fine = RenderRayNet(args.netdepth_fine, args.netwidth_fine, position_encoder.output_dim * 3,
                              direction_encoder.output_dim * 3, skips=args.skips_fine)

    if args.model_type == "smpl_nerf":
        human_pose_encoder = PositionalEncoder(args.number_frequencies_pose, args.use_identity_pose)
        positions_dim = position_encoder.output_dim if args.human_pose_encoding else 1
        human_pose_dim = human_pose_encoder.output_dim if args.human_pose_encoding else 1
        model_warp_field = WarpFieldNet(args.netdepth_warp, args.netwidth_warp, positions_dim * 3,
                                        human_pose_dim * 2)

        solver = SmplNerfSolver(model_coarse, model_fine, model_warp_field, position_encoder, direction_encoder,
                                human_pose_encoder, train_data.canonical_smpl, args, torch.optim.Adam,
                                torch.nn.MSELoss())
        solver.train(train_loader, val_loader, train_data.h, train_data.w)
        save_run(os.path.join(solver.writer.log_dir, args.experiment_name + '.pkl'), model_coarse, model_fine,
                 train_data,
                 solver, parser, model_warp_field)
    elif args.model_type == 'smpl':
        solver = SmplSolver(model_coarse, model_fine, position_encoder, direction_encoder,
                            args, torch.optim.Adam,
                            torch.nn.MSELoss())
        solver.train(train_loader, val_loader, train_data.h, train_data.w)
        save_run(os.path.join(solver.writer.log_dir, args.experiment_name + '.pkl'), model_coarse, model_fine,
                 train_data,
                 solver, parser)
    elif args.model_type == 'nerf':
        solver = NerfSolver(model_coarse, model_fine, position_encoder, direction_encoder, args, torch.optim.Adam,
                            torch.nn.MSELoss())
        solver.train(train_loader, val_loader, train_data.h, train_data.w)
        save_run(os.path.join(solver.writer.log_dir, args.experiment_name + '.pkl'), model_coarse, model_fine,
                 train_data,
                 solver, parser)
    elif args.model_type == 'append_to_nerf':
        human_pose_encoder = PositionalEncoder(args.number_frequencies_pose, args.use_identity_pose)
        human_pose_dim = human_pose_encoder.output_dim if args.human_pose_encoding else 1
        model_coarse = RenderRayNet(args.netdepth, args.netwidth, position_encoder.output_dim * 3,
                                    direction_encoder.output_dim * 3, human_pose_dim * 2,
                                    skips=args.skips)
        model_fine = RenderRayNet(args.netdepth_fine, args.netwidth_fine, position_encoder.output_dim * 3,
                                  direction_encoder.output_dim * 3, human_pose_dim * 2,
                                  skips=args.skips_fine)
        solver = AppendToNerfSolver(model_coarse, model_fine, position_encoder, direction_encoder, human_pose_encoder,
                                    args, torch.optim.Adam,
                                    torch.nn.MSELoss())
        solver.train(train_loader, val_loader, train_data.h, train_data.w)
        save_run(os.path.join(solver.writer.log_dir, args.experiment_name + '.pkl'), model_coarse, model_fine,
                 train_data,
                 solver, parser)


if __name__ == '__main__':
    train()
