import torch
import numpy as np

from torch.nn import functional as F
from datasets.sub_dataset import SubDataset
from models.dynamic_pipeline import DynamicPipeline
from solver.nerf_solver import NerfSolver
from utils import PositionalEncoder, tensorboard_rerenders, vedo_data, modified_softmax, raw2outputs
from torch.utils.data import TensorDataset, DataLoader


class ImageWiseSolver(NerfSolver):
    '''
    Solver for a dataset of images and the corresponding rays such that an the warp of each ray can be calculated and the
    batching happens in an image.
    '''

    def __init__(self, model_coarse, model_fine, smpl_estimator, smpl_model, positions_encoder: PositionalEncoder,
                 directions_encoder: PositionalEncoder, args,
                 optim=torch.optim.Adam, loss_func=torch.nn.MSELoss()):
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.smpl_estimator = smpl_estimator.to(self.device)
        self.smpl_model = smpl_model.to(self.device)
        self.position_encoder = positions_encoder
        self.direction_encoder = directions_encoder
        self.canonical_pose = torch.zeros([1, 69], device=self.device)
        super(ImageWiseSolver, self).__init__(model_coarse, model_fine, positions_encoder, directions_encoder, args,
                                              optim, loss_func)
        print('estimator params', list(self.smpl_estimator.parameters()))
        self.optim = optim(
            list(model_coarse.parameters()) + list(model_fine.parameters()) + list(self.smpl_estimator.parameters()),
            **self.optim_args_merged)

    def init_pipeline(self):
        return None

    def train(self, train_loader, val_loader, h: int, w: int):
        """
        Train coarse and fine model on training data and run validation

        Parameters
        ----------
        train_loader : training data loader object.
        val_loader : validation data loader object.
        h : int
            height of images.
        w : int
            width of images.
        """
        args = self.args

        print('START TRAIN.')

        for epoch in range(args.num_epochs):  # loop over the dataset multiple times
            self.model_coarse.train()
            self.model_fine.train()
            self.smpl_estimator.train()
            train_loss = 0
            train_coarse_loss = 0
            train_fine_loss = 0
            for i, image_batch in enumerate(train_loader):
                for j, element in enumerate(image_batch):
                    image_batch[j] = element[0].to(self.device)
                ray_samples, samples_translations, samples_directions, z_vals, rgb = image_batch

                sub_dataset = SubDataset(ray_samples, samples_translations, samples_directions, rgb)
                dataloader = DataLoader(sub_dataset, args.batchsize, shuffle=True, num_workers=0)
                iter_per_image = len(dataloader)

                goal_pose, betas = self.smpl_estimator(1)
                # print('betas ', self.smpl_estimator.betas)
                # print('expression ', self.smpl_estimator.expression)
                # print('goal_poses', self.smpl_estimator.goal_poses)

                canonical_model = self.smpl_model(betas=betas, return_verts=True,
                                                  body_pose=self.canonical_pose)  # [number_vertices, 3]
                goal_models = self.smpl_model(betas=betas, return_verts=True, body_pose=goal_pose)

                goal_vertices = goal_models.vertices  # [1, number_vertices, 3]
                warp = canonical_model.vertices - goal_vertices  # [1, number_vertices, 3]
                warp = warp.expand(args.batchsize, -1, -1)
                for j, ray_batch in enumerate(dataloader):
                    for c, element in enumerate(ray_batch):
                        ray_batch[c] = element.to(self.device)
                    ray_samples, rays_translation, rays_direction, rgb_truth = ray_batch

                    distances = ray_samples[:, :, None, :] - goal_vertices[:, None, :, :].expand(
                        (-1, ray_samples.shape[1], -1, -1))  # [batchsize, number_samples, number_vertices, 3]
                    distances = torch.norm(distances, dim=-1)  # [batchsize, number_samples, number_vertices]
                    attentions_1 = distances - self.args.warp_radius  # [batchsize, number_samples, number_vertices]
                    attentions_2 = F.relu(-attentions_1)
                    # print('iter')
                    # attentions_2.register_hook(lambda x: print_number_nans('pre', x))
                    # attentions_2.register_hook(lambda x: print_max('pre',x))

                    attentions_3 = modified_softmax(self.args.warp_temperature * attentions_2)
                    # attentions_3.register_hook(lambda x: print_max('post',x))

                    warps = warp[:, None, :, :] * attentions_3[:, :, :,
                                                  None]  # [batchsize, number_samples, number_vertices, 3]
                    warps = warps.sum(dim=-2)  # [batchsize, number_samples, 3]
                    warped_samples = ray_samples + warps

                    samples_encoding = self.position_encoder.encode(warped_samples)

                    coarse_samples_directions = warped_samples - rays_translation[:, None,
                                                                 :]  # [batchsize, number_coarse_samples, 3]
                    samples_directions_norm = coarse_samples_directions / torch.norm(coarse_samples_directions, dim=-1,
                                                                                     keepdim=True)
                    directions_encoding = self.direction_encoder.encode(samples_directions_norm)
                    # flatten the encodings from [batchsize, number_coarse_samples, encoding_size] to [batchsize * number_coarse_samples, encoding_size] and concatenate
                    inputs = torch.cat([samples_encoding.view(-1, samples_encoding.shape[-1]),
                                        directions_encoding.view(-1, directions_encoding.shape[-1])], -1)
                    raw_outputs = self.model_coarse(inputs)  # [batchsize * number_coarse_samples, 4]
                    raw_outputs = raw_outputs.view(samples_encoding.shape[0], samples_encoding.shape[1],
                                                   raw_outputs.shape[-1])  # [batchsize, number_coarse_samples, 4]
                    rgb, weights, densities = raw2outputs(raw_outputs, z_vals, coarse_samples_directions, self.args)

                    self.optim.zero_grad()

                    loss = self.loss_func(rgb, rgb_truth)

                    loss.backward(retain_graph=True)

                    self.optim.step()

                    loss_item = loss.item()

                    if j % args.log_iterations == args.log_iterations - 1:
                        print('[Epoch %d, Iteration %5d/%5d] TRAIN loss: %.7f' %
                              (epoch + 1, j + 1, iter_per_image, loss_item))

                    train_loss += loss_item
            print('[Epoch %d] Average loss of Epoch: %.7f' %
                  (epoch + 1, train_loss / iter_per_image * len(train_loader)))

            self.model_coarse.eval()
            self.model_fine.eval()
            self.smpl_estimator.eval()
            val_loss = 0
            rerender_images = []
            ground_truth_images = []
            samples = []
            warp_history = []
            ray_warp_magnitudes = []
            densities_list = []
            for i, image_batch in enumerate(val_loader):
                for j, element in enumerate(image_batch):
                    image_batch[j] = element[0].to(self.device)
                ray_samples, samples_translations, samples_directions, z_vals, rgb = image_batch

                sub_dataset = SubDataset(ray_samples, samples_translations, samples_directions, rgb)
                dataloader = DataLoader(sub_dataset, args.batchsize, shuffle=True, num_workers=0)
                iter_per_image_val = len(dataloader)
                goal_pose, betas = self.smpl_estimator(1)

                canonical_model = self.smpl_model(betas=betas, return_verts=True,
                                                  body_pose=self.canonical_pose)  # [number_vertices, 3]
                goal_models = self.smpl_model(betas=betas, return_verts=True, body_pose=goal_pose)

                goal_vertices = goal_models.vertices  # [1, number_vertices, 3]
                warp = canonical_model.vertices - goal_vertices  # [1, number_vertices, 3]
                warp = warp.expand(args.batchsize, -1, -1)  # [batchsize, number_vertices, 3]
                image_warps = []
                image_densities = []
                image_samples = []
                for j, ray_batch in enumerate(dataloader):
                    for j, element in enumerate(ray_batch):
                        ray_batch[j] = element.to(self.device)
                    ray_samples, rays_translation, rays_direction, rgb_truth = ray_batch

                    distances = ray_samples[:, :, None, :] - goal_vertices[:, None, :, :].expand(
                        (-1, ray_samples.shape[1], -1, -1))  # [batchsize, number_samples, number_vertices, 3]
                    distances = torch.norm(distances, dim=-1)  # [batchsize, number_samples, number_vertices]
                    attentions_1 = distances - self.args.warp_radius  # [batchsize, number_samples, number_vertices]
                    attentions_2 = F.relu(-attentions_1)

                    attentions_3 = modified_softmax(self.args.warp_temperature * attentions_2)

                    warps = warp[:, None, :, :] * attentions_3[:, :, :,
                                                  None]  # [batchsize, number_samples, number_vertices, 3]
                    warps = warps.sum(dim=-2)  # [batchsize, number_samples, 3]
                    warped_samples = ray_samples + warps

                    samples_encoding = self.position_encoder.encode(warped_samples)

                    coarse_samples_directions = warped_samples - rays_translation[:, None,
                                                                 :]  # [batchsize, number_coarse_samples, 3]
                    samples_directions_norm = coarse_samples_directions / torch.norm(coarse_samples_directions, dim=-1,
                                                                                     keepdim=True)
                    directions_encoding = self.direction_encoder.encode(samples_directions_norm)
                    # flatten the encodings from [batchsize, number_coarse_samples, encoding_size] to [batchsize * number_coarse_samples, encoding_size] and concatenate
                    inputs = torch.cat([samples_encoding.view(-1, samples_encoding.shape[-1]),
                                        directions_encoding.view(-1, directions_encoding.shape[-1])], -1)
                    raw_outputs = self.model_coarse(inputs)  # [batchsize * number_coarse_samples, 4]
                    raw_outputs = raw_outputs.view(samples_encoding.shape[0], samples_encoding.shape[1],
                                                   raw_outputs.shape[-1])  # [batchsize, number_coarse_samples, 4]
                    rgb, weights, densities = raw2outputs(raw_outputs, z_vals, coarse_samples_directions, self.args)

                    loss = self.loss_func(rgb, rgb_truth)

                    val_loss += loss.item()

                    ground_truth_images.append(rgb_truth.detach().cpu().numpy())
                    rerender_images.append(rgb.detach().cpu().numpy())
                    samples.append(ray_samples.detach().cpu().numpy())
                    image_samples.append(ray_samples.detach().cpu().numpy())
                    warp_history.append(warps.detach().cpu().numpy())
                    image_warps.append(warps.detach().cpu().numpy())
                    densities_list.append(densities.detach().cpu().numpy())
                    image_densities.append(densities.detach().cpu().numpy())
                    warp_magnitude = np.linalg.norm(warp.detach().cpu(), axis=-1)  # [batchsize, number_samples]
                    ray_warp_magnitudes.append(warp_magnitude.mean(axis=1))  # mean over the samples => [batchsize]

                vedo_data(self.writer, np.concatenate(image_densities).reshape(-1),
                          np.concatenate(image_samples).reshape(-1, 3),
                          image_warps=np.concatenate(image_warps).reshape(-1, 3), epoch=epoch + 1,
                          image_idx=i)
            if len(val_loader) != 0:
                rerender_images = np.concatenate(rerender_images, 0).reshape((-1, h, w, 3))
                ground_truth_images = np.concatenate(ground_truth_images).reshape((-1, h, w, 3))
                ray_warp_magnitudes = np.concatenate(ray_warp_magnitudes).reshape((-1, h, w))

            tensorboard_rerenders(self.writer, args.number_validation_images, rerender_images, ground_truth_images,
                                  step=epoch + 1, ray_warps=ray_warp_magnitudes)

            print('[Epoch %d] VAL loss: %.7f' % (
            epoch + 1, val_loss / (len(val_loader) * iter_per_image_val or not len(val_loader) * iter_per_image_val)))
            self.writer.add_scalars('Loss Curve', {'train loss': train_loss / iter_per_image * len(train_loader),
                                                   'val loss': val_loss / (
                                                               len(val_loader) * iter_per_image_val or not len(
                                                           val_loader) * iter_per_image_val)},
                                    epoch + 1)
        print('FINISH.')
