import numpy as np
import torch
from pytorch_fid.fid_score import calculate_frechet_distance
from torch.nn.functional import adaptive_avg_pool2d
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

from evaluate import calculate_correlation_coefficient
from src.tool.log import write_iteration_log


def img_transform(img):
    img = img.numpy().transpose((0, 2, 3, 1))
    mean = np.array([0.485, 0.456, 0.406])
    std = np.array([0.229, 0.224, 0.225])
    img = std * img + mean
    img = np.clip(img, 0, 1)
    return img


def get_activations(imgs, model):
    with torch.no_grad():
        pred = model(imgs)[0]

    # If model output is not scalar, apply global spatial average pooling.
    # This happens if you choose a dimensionality not equal 2048.
    if pred.size(2) != 1 or pred.size(3) != 1:
        pred = adaptive_avg_pool2d(pred, output_size=(1, 1))

    return pred.squeeze(3).squeeze(2).cpu().numpy()


def train_phase1(dataloader,
                 model,
                 optimizer,
                 loss,
                 cfg,
                 start_iteration,
                 dataset_size,
                 writer: SummaryWriter,
                 device=torch.device('cpu')):
    record = {
        'gt_scores': [],
        'pred_scores': [],
        'inception_real_pred': [],
        'inception_fake_pred': []
    }

    result = {
        'real_clf': 0,
        'real_qual': 0,
        'fake_clf': 0,
        'fake_qual': 0,
        'cont': 0
    }

    model['netG'].train()
    model['netD'].train()

    with tqdm(dataloader) as tepoch:
        for iteration, (ref_imgs, dist_imgs, scores, categories, origin_scores) in enumerate(tepoch):
            ref_imgs = ref_imgs.to(device)
            dist_imgs = dist_imgs.to(device)
            scores = scores.to(device).float()
            categories = categories.to(device)

            # Format batch
            bs = ref_imgs.size(0)

            """
            Discriminator
            """
            optimizer['optimizerD'].zero_grad()

            """
            Discriminator with real image
            """
            validity = torch.full((bs,), 1, dtype=torch.float, device=device)

            pred_validity, pred_categories, pred_scores = model['netD'](ref_imgs, dist_imgs)

            record['D_x'] = pred_validity.mean().item()

            errD_real_adv = loss['bce_loss'](pred_validity, validity)
            errD_real_clf = loss['ce_loss'](pred_categories, categories)
            errD_real_qual = loss['mse_loss'](pred_scores, scores)

            record['errD_real_adv'] = errD_real_adv.item()
            record['errD_real_clf'] = errD_real_clf.item()
            record['errD_real_qual'] = errD_real_qual.item()

            errD_real = \
                cfg.TRAIN.CRITERION_WEIGHT.ERRD_REAL_ADV * errD_real_adv + \
                cfg.TRAIN.CRITERION_WEIGHT.ERRD_REAL_CLF * errD_real_clf + \
                cfg.TRAIN.CRITERION_WEIGHT.ERRD_REAL_QUAL * errD_real_qual

            # Record original scores and predict scores
            record['gt_scores'].append(origin_scores)
            record['pred_scores'].append(pred_scores.cpu().detach())

            """
            Discriminator with fake image
            """
            # Generate batch of latent vectors
            noise = torch.randn(bs, cfg.MODEL.LATENT_DIM, device=device)

            fake_imgs = model['netG'](ref_imgs,
                                      noise,
                                      scores.view(bs, -1),
                                      categories.view(bs, -1).float())
            validity = torch.full((bs,), 0, dtype=torch.float, device=device)

            pred_validity, pred_categories, _ = model['netD'](ref_imgs, fake_imgs.detach())

            record['D_G_z1'] = pred_validity.mean().item()

            errD_fake_adv = loss['bce_loss'](pred_validity, validity)
            errD_fake_clf = loss['ce_loss'](pred_categories, categories)

            record['errD_fake_adv'] = errD_fake_adv.item()
            record['errD_fake_clf'] = errD_fake_clf.item()

            errD_fake = cfg.TRAIN.CRITERION_WEIGHT.ERRD_FAKE_ADV * errD_fake_adv + cfg.TRAIN.CRITERION_WEIGHT.ERRD_FAKE_CLF * errD_fake_clf

            errD = errD_real + errD_fake
            record['errD'] = errD.item()

            errD.backward()
            optimizer['optimizerD'].step()

            """
            Generator
            """
            optimizer['optimizerG'].zero_grad()

            validity = torch.full((bs,), 1, dtype=torch.float, device=device)

            pred_validity, pred_categories, pred_scores = model['netD'](ref_imgs, fake_imgs)

            record['D_G_z2'] = pred_validity.mean().item()

            errG_adv = loss['bce_loss'](pred_validity, validity)
            errG_clf = loss['ce_loss'](pred_categories, categories)
            errG_qual = loss['mse_loss'](pred_scores, scores)
            errG_cont = loss['l1_loss'](fake_imgs, dist_imgs)

            record['errG_adv'] = errG_adv.item()
            record['errG_clf'] = errG_clf.item()
            record['errG_qual'] = errG_qual.item()
            record['errG_cont'] = errG_cont.item()

            errG = \
                cfg.TRAIN.CRITERION_WEIGHT.ERRG_ADV * errG_adv + \
                cfg.TRAIN.CRITERION_WEIGHT.ERRG_CLF * errG_clf + \
                cfg.TRAIN.CRITERION_WEIGHT.ERRG_QUAL * errG_qual + \
                cfg.TRAIN.CRITERION_WEIGHT.ERRG_CONT * errG_cont

            record['errG'] = errG.item()

            errG.backward()
            optimizer['optimizerG'].step()

            record['real_imgs'] = img_transform(dist_imgs.cpu().detach())
            record['fake_imgs'] = img_transform(fake_imgs.cpu().detach())

            """
            Record activations
            """
            with torch.no_grad():
                record['inception_real_pred'].append(get_activations(dist_imgs, model['inception']))
                record['inception_fake_pred'].append(get_activations(fake_imgs.detach(), model['inception']))

            """
            Show logs
            """
            tepoch.set_postfix({
                'Loss_D': record['errD'],
                'Loss_G': record['errG'],
                'D(x)': record['D_x'],
                'D(G(z))': f'{record["D_G_z1"]: .4f}/{record["D_G_z2"]: .4f}'
            })

            if (start_iteration + iteration) % 100 == 0:
                write_iteration_log(writer, record, start_iteration + iteration, cfg)

            """
            Record epoch loss
            """
            result['real_clf'] += record['errD_real_clf'] * bs
            result['real_qual'] += record['errD_real_qual'] * bs
            result['fake_clf'] += record['errG_clf'] * bs
            result['fake_qual'] += record['errG_qual'] * bs
            result['cont'] += record['errG_cont'] * bs

    result['real_clf'] /= dataset_size
    result['real_qual'] /= dataset_size
    result['fake_clf'] /= dataset_size
    result['fake_qual'] /= dataset_size
    result['cont'] /= dataset_size

    """
    Calculate FID score
    """
    real_mu = np.mean(np.concatenate(record['inception_real_pred']), axis=0)
    fake_mu = np.mean(np.concatenate(record['inception_fake_pred']), axis=0)

    real_sigma = np.cov(np.concatenate(record['inception_real_pred']), rowvar=False)
    fake_sigma = np.cov(np.concatenate(record['inception_fake_pred']), rowvar=False)

    result['FID'] = calculate_frechet_distance(real_mu, real_sigma, fake_mu, fake_sigma)

    """
    Calculate correlation coefficient
    """
    result['PLCC'], result['SRCC'], result['KRCC'] = \
        calculate_correlation_coefficient(
            torch.cat(record['gt_scores']).numpy(),
            torch.cat(record['pred_scores']).numpy()
        )

    return result


def train_phase2(dataloader, model, optimizer, loss, cfg, dataset_size, device=torch.device('cpu')):
    record = {
        'gt_scores': [],
        'pred_scores': []
    }

    result = {
        'real_loss': 0,
        'fake_loss': 0
    }

    with tqdm(dataloader) as tepoch:
        for iteration, (ref_imgs, dist_imgs, scores, categories, origin_scores) in enumerate(tepoch):
            ref_imgs = ref_imgs.to(device)
            dist_imgs = dist_imgs.to(device)
            scores = scores.to(device).float()
            categories = categories.to(device)

            # Format batch
            bs = ref_imgs.size(0)

            optimizer.zero_grad()

            """
            Deal with Real Distorted Images
            """
            _, _, pred_scores = model['netD'](ref_imgs, dist_imgs)

            real_loss = loss['mse_loss'](pred_scores, scores)

            # Record original scores and predict scores
            record['gt_scores'].append(origin_scores)
            record['pred_scores'].append(pred_scores.cpu().detach())

            """
            Deal with Fake Distorted Images
            """
            # Generate batch of latent vectors
            noise = torch.randn(bs, cfg.MODEL.LATENT_DIM, device=device)

            fake_imgs = model['netG'](ref_imgs,
                                      noise,
                                      scores.view(bs, -1),
                                      categories.view(bs, -1).float())

            _, _, pred_scores = model['netD'](ref_imgs, fake_imgs.detach())

            fake_loss = loss['mse_loss'](pred_scores, scores)

            total_loss = real_loss + fake_loss
            total_loss.backward()
            optimizer.step()

            result['real_loss'] += real_loss.item() * bs
            result['fake_loss'] += fake_loss.item() * bs

            # Show training message
            tepoch.set_postfix({
                'Real Loss': real_loss.item(),
                'Fake Loss': fake_loss.item(),
                'Total Loss': total_loss.item()
            })

    result['real_loss'] /= dataset_size
    result['fake_loss'] /= dataset_size

    """
    Calculate correlation coefficient
    """
    result['PLCC'], result['SRCC'], result['KRCC'] = \
        calculate_correlation_coefficient(
            torch.cat(record['gt_scores']).numpy(),
            torch.cat(record['pred_scores']).numpy()
        )

    return result