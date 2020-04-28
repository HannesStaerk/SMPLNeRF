## Setup Baseline
- Clone Pix2Pix repo (TODO: install dependencies):
```bash
mkdir baseline/
cd baseline/
git clone https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix.git
cd ..
```
- Create Dataset with (RGB, depth)-pairs
```bash
python create_dataset_pix2pix.py --dataset=pix2pix --save_dir=baseline/pytorch-CycleGAN-and-pix2pix/datasets/smpl --resolution=128 --start_angle=-90 --end_angle=90 --number_steps=10
```
- Train Pix2Pix on datasets (set name for experiment, set gpu_ids=-1 for CPU)
```bash
python train.py --gpu_ids=0 --model=pix2pix --dataroot=datasets/data_pix2pix --name=SMPL_pix2pix --direction=BtoA --save_epoch_freq=50
```