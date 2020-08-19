import torch

from models.nerf_pipeline import NerfPipeline
from utils import PositionalEncoder, raw2outputs, fine_sampling


class AppendVerticesPipeline(NerfPipeline):

    def __init__(self, model_coarse, model_fine, smpl_estimator, smpl_model, args, position_encoder: PositionalEncoder,
                 direction_encoder: PositionalEncoder):
        super(AppendVerticesPipeline, self).__init__(model_coarse, model_fine, args, position_encoder, direction_encoder)
        self.smpl_estimator = smpl_estimator
        self.smpl_model = smpl_model
        self.global_orient = torch.zeros([1, 3], device=self.device)

    def forward(self, data):
        """
            Volumetric rendering with NeRF and pose parameters concatenated to nerf input.

            Returns
            -------
            rgb : torch.Tensor ([batch_size, 3])
                Estimated RGB color with coarse net.
            rgb_fine : torch.Tensor ([batch_size, 3])
                Estimated RGB color with fine net.
            """
        ray_samples, ray_translation, ray_direction, z_vals, images, rb_truth = data

        goal_poses, betas = self.smpl_estimator(images)
        # print('betas ', self.smpl_estimator.betas)
        # print('expression ', self.smpl_estimator.expression)
        # print('goal_poses', self.smpl_estimator.goal_poses)

        # right now expanding and using self.global_orient_instead of setting the correct batchisze for the smpl model since the same smpl model is used in train and validation with different batch sizes (the batchisze of the smpl_model is 1 by default)
        global_orient = self.global_orient.expand(len(ray_samples), -1)

        goal_models = self.smpl_model(betas=betas, return_verts=True, body_pose=goal_poses,
                                      global_orient=global_orient)
        vertices = goal_models.vertices
        vertices_flat = vertices.reshape(vertices.shape[0],-1) # [batchsize, 6890*3]


        vertices_flat = vertices_flat[..., None, :].expand(vertices_flat.shape[0],
                                                                ray_samples.shape[1],
                                                                vertices_flat.shape[-1]) # [batchsize,  number_coarse_samples, 6890*3]

        # get values for coarse network and run them through the coarse network
        samples_encoding = self.position_encoder.encode(ray_samples)
        coarse_samples_directions = ray_direction[..., None, :].expand(ray_direction.shape[0], ray_samples.shape[1],
                                                                       ray_direction.shape[
                                                                           -1])  # [batchsize, number_coarse_samples, 3]
        samples_directions_norm = coarse_samples_directions / torch.norm(coarse_samples_directions, dim=-1,
                                                                         keepdim=True)
        directions_encoding = self.direction_encoder.encode(samples_directions_norm)
        # flatten the encodings from [batchsize, number_coarse_samples, encoding_size] to [batchsize * number_coarse_samples, encoding_size] and concatenate
        inputs = torch.cat([vertices_flat.reshape(-1, vertices_flat.shape[-1]),
                            samples_encoding.view(-1, samples_encoding.shape[-1]),
                            directions_encoding.view(-1, directions_encoding.shape[-1])], -1)
        raw_outputs = self.model_coarse(inputs)  # [batchsize * number_coarse_samples, 4]
        raw_outputs = raw_outputs.view(samples_encoding.shape[0], samples_encoding.shape[1],
                                       raw_outputs.shape[-1])  # [batchsize, number_coarse_samples, 4]
        rgb, weights, densities = raw2outputs(raw_outputs, z_vals, coarse_samples_directions, self.args)
        if not self.args.run_fine:
            return rgb, rgb, ray_samples, densities

        # get values for the fine network and run them through the fine network
        z_vals, ray_samples_fine = fine_sampling(ray_translation, ray_direction, z_vals, weights,
                                                 self.args.number_fine_samples)  # [batchsize, number_coarse_samples + number_fine_samples, 3]
        samples_encoding_fine = self.position_encoder.encode(ray_samples_fine)

        vertices_flat = vertices_flat[..., None, :].expand(vertices_flat.shape[0],
                                                           ray_samples_fine.shape[1],
                                                           vertices_flat.shape[
                                                               -1])  # [batchsize,  number_coarse_samples, 6890*3]

        # expand directions and translations to the number of coarse samples + fine_samples
        directions_encoding_fine = directions_encoding[..., :1, :].expand(directions_encoding.shape[0],
                                                                          ray_samples_fine.shape[1],
                                                                          directions_encoding.shape[-1])
        inputs_fine = torch.cat([vertices_flat.reshape(-1, vertices_flat.shape[-1]),
                                 samples_encoding_fine.view(-1, samples_encoding_fine.shape[-1]),
                                 directions_encoding_fine.reshape(-1, directions_encoding_fine.shape[-1])], -1)
        raw_outputs_fine = self.model_fine(
            inputs_fine)  # [batchsize * (number_coarse_samples + number_fine_samples), 4]
        raw_outputs_fine = raw_outputs_fine.reshape(samples_encoding_fine.shape[0], samples_encoding_fine.shape[1],
                                                    raw_outputs_fine.shape[
                                                        -1])  # [batchsize, number_coarse_samples + number_fine_samples, 4]
        # expand directions and translations to the number of coarse samples + fine_samples
        fine_samples_directions = ray_direction[..., None, :].expand(ray_direction.shape[0],
                                                                     ray_samples_fine.shape[1],
                                                                     ray_direction.shape[-1])
        rgb_fine, _, densities = raw2outputs(raw_outputs_fine, z_vals, fine_samples_directions, self.args)

        return rgb, rgb_fine, ray_samples_fine, densities
