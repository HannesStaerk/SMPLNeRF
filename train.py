import os

import torch
from torch.utils.data import Subset
from torchvision.transforms import transforms
from config_parser import config_parser
from datasets.rays_from_images_dataset import RaysFromImagesDataset
from datasets.transforms import CoarseSampling, ToTensor, NormalizeRGB
from models.render_ray_net import RenderRayNet
from solver.nerf_solver import NerfSolver
import numpy as np
from torchsummary import summary

from utils import PositionalEncoder, save_run

np.random.seed(0)



def train():
    parser = config_parser()
    args = parser.parse_args()

    transform = transforms.Compose(
        [NormalizeRGB(), CoarseSampling(args.near, args.far, args.number_coarse_samples), ToTensor()])

    dataset = RaysFromImagesDataset(args.train_directory, args.train_camera_transforms, transform)
    val_data = RaysFromImagesDataset(args.val_directory, args.val_camera_transforms, transform)

    train_loader = torch.utils.data.DataLoader(dataset, batch_size=args.batchsize, shuffle=True, num_workers=0)
    val_loader = torch.utils.data.DataLoader(val_data, batch_size=args.batchsize_val, shuffle=False, num_workers=0)
    position_encoder = PositionalEncoder(args.number_frequencies_postitional, args.use_identity_positional)
    direction_encoder = PositionalEncoder(args.number_frequencies_directional, args.use_identity_directional)
    model_coarse = RenderRayNet(args.netdepth, args.netwidth, position_encoder.output_dim * 3,
                                direction_encoder.output_dim * 3)
    model_fine = RenderRayNet(args.netdepth_fine, args.netwidth_fine, position_encoder.output_dim * 3,
                              direction_encoder.output_dim * 3)
    print(model_coarse)
    solver = NerfSolver(position_encoder, direction_encoder, args, torch.optim.Adam,
                        torch.nn.MSELoss())
    solver.train(model_coarse, model_fine, train_loader, val_loader, dataset.h, dataset.w)

    save_run(os.path.join(solver.writer.log_dir, args.experiment_name + '.pkl'), model_coarse, model_fine, dataset,
             solver, parser)


if __name__ == '__main__':
    train()
