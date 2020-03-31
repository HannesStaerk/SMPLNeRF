import os.path as osp
import argparse

import numpy as np
import torch

import smplx



model = smplx.create("models", model_type='smplx')

print(model.parameters())
betas = torch.randn([1, 10], dtype=torch.float32)
expression = torch.randn([1, 10], dtype=torch.float32)

output = model(betas=betas, expression=expression,
               return_verts=True)
vertices = output.vertices.detach().cpu().numpy().squeeze()
joints = output.joints.detach().cpu().numpy().squeeze()

print('Vertices shape =', vertices.shape)
print('Joints shape =', joints.shape)


import pyrender
import trimesh
vertex_colors = np.ones([vertices.shape[0], 4]) * [0.3, 0.3, 0.3, 0.8]
tri_mesh = trimesh.Trimesh(vertices, model.faces,
                           vertex_colors=vertex_colors)

mesh = pyrender.Mesh.from_trimesh(tri_mesh)

scene = pyrender.Scene()
scene.add(mesh)


sm = trimesh.creation.uv_sphere(radius=0.005)
sm.visual.vertex_colors = np.random.uniform(size=sm.vertices.shape)
tfs = np.tile(np.eye(4), (len(joints), 1, 1))
tfs[:, :3, 3] = joints

joints_pcl = pyrender.Mesh.from_trimesh(sm, poses=tfs)
scene.add(joints_pcl)

pyrender.Viewer(scene, use_raymond_lighting=True)
