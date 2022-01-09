import argparse
import os
import shutil
from pathlib import Path

import torch
import torch.multiprocessing
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts
from torch.utils.tensorboard import SummaryWriter

from src.config.config_phase2 import get_cfg_defaults
from src.data.dataset import create_dataloaders
from src.modeling.evaluate import evaluate_phase2
from src.modeling.module import Generator, MultiTask
from src.tool.train import train_phase2


def main(cfg):
    if cfg.TRAIN.WEIGHT_DIR and not os.path.isdir(cfg.TRAIN.WEIGHT_DIR):
        os.makedirs(cfg.TRAIN.WEIGHT_DIR)

    if cfg.TRAIN.LOG_DIR and os.path.isdir(cfg.TRAIN.LOG_DIR):
        shutil.rmtree(cfg.TRAIN.LOG_DIR)

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    dataloaders, datasets_size = create_dataloaders(
        Path(cfg.DATASETS.ROOT_DIR),
        phase=2,
        batch_size=cfg.DATASETS.BATCH_SIZE,
        num_workers=cfg.DATASETS.NUM_WORKERS
    )

    # Set Up Model
    model = {
        'netG': Generator().to(device),
        'netD': MultiTask(cfg).to(device)
    }

    if cfg.TRAIN.RESUME.NET_G:
        model['netG'].load_state_dict(torch.load(cfg.TRAIN.RESUME.NET_G))
    model['netG'].eval()
    if cfg.TRAIN.RESUME.NET_D:
        model['netD'].load_state_dict(torch.load(cfg.TRAIN.RESUME.NET_D))

    loss = {
        'mse_loss': nn.MSELoss()
    }

    optimizer = optim.Adam(model['netD'].parameters(), lr=cfg.TRAIN.LEARNING_RATE)
    scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=1, T_mult=2)
    if cfg.TRAIN.START_EPOCH != 0:
        scheduler.step(cfg.TRAIN.START_EPOCH)

    if cfg.TRAIN.LOG_DIR:
        writer = SummaryWriter(log_dir=cfg.TRAIN.LOG_DIR)
    else:
        writer = SummaryWriter()

    for epoch in range(cfg.TRAIN.START_EPOCH, cfg.TRAIN.START_EPOCH + cfg.TRAIN.NUM_EPOCHS):
        print(f'Epoch {epoch + 1}/{cfg.TRAIN.START_EPOCH + cfg.TRAIN.NUM_EPOCHS}')
        print('-' * 10)

        results = {
            'train': train_phase2(
                dataloaders['train'],
                model,
                optimizer,
                loss,
                cfg,
                datasets_size['train'],
                device
            ),
            'val': evaluate_phase2(
                dataloaders['val'],
                model,
                loss,
                cfg,
                datasets_size['val'],
                device
            )
        }

        scheduler.step()

        writer.add_scalars(
            'Loss', {
                'train_real': results['train']['real_loss'],
                'val_real': results['val']['real_loss'],
                'train_fake': results['train']['fake_loss'],
                'val_fake': results['val']['fake_loss']
            },
            epoch + 1
        )
        writer.add_scalars('PLCC', {x: results[x]['PLCC'] for x in ['train', 'val']}, epoch + 1)
        writer.add_scalars('SRCC', {x: results[x]['SRCC'] for x in ['train', 'val']}, epoch + 1)
        writer.add_scalars('KRCC', {x: results[x]['KRCC'] for x in ['train', 'val']}, epoch + 1)
        writer.flush()

        if cfg.TRAIN.WEIGHT_DIR:
            torch.save(model['netD'].state_dict(), os.path.join(cfg.TRAIN.WEIGHT_DIR, f'netD_epoch{epoch + 1}.pth'))

    writer.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--config', type=str, help='Configuration YAML file for train phase1')
    args = parser.parse_args()

    cfg = get_cfg_defaults()
    try:
        cfg.merge_from_file(args.config)
    except:
        print('Using default configuration file')
        print('Incorrect to train phase2 without loading phase1 weight')

    cfg.freeze()

    main(cfg)