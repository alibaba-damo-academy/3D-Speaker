import os
import argparse
import time
import sys
sys.path.append('/mnt/workspace/gitlab/3D-Speaker')
os.chdir('/mnt/workspace/gitlab/3D-Speaker/egs/voxceleb-sv/v1')

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.distributed as dist

from speakerlab.utils.utils import set_seed, get_logger, AverageMeters, ProgressMeter, accuracy
from speakerlab.utils.config import build_config
from speakerlab.utils.builder import build
from speakerlab.utils.epoch import EpochCounter, EpochLogger
from speakerlab.utils.test_in_train import CosineTestIntraEpoch


parser = argparse.ArgumentParser(description='Speaker Network Training')
parser.add_argument('--config', default='', type=str, help='Config file')
parser.add_argument('--resume', default=True, type=bool, help='if or not to resume from recent checkpoint')
parser.add_argument('--seed', default=1234, type=int, help='seed for initializing training. ')
parser.add_argument('--gpu', nargs='+', help='GPU id to use.')

def main():
    args_list = ['--config', 'conf/cam++.yaml', '--gpu', '0',
    '--data', 'data/vox2_dev/train.csv', '--noise', 'data/musan/wav.scp', '--reverb','data/rirs/wav.scp', '--exp_dir','exp/cam++-2']
    # kwargs = {'data': 'data/vox2_dev/train.csv', 'noise': 'data/musan/wav.scp', 'reverb':'data/rirs/wav.scp', 'exp_dir':'exp/cam++'}
    args, overrides = parser.parse_known_args(args_list)
    config = build_config(args.config, overrides)

    os.environ['LOCAL_RANK'] = '0'
    os.environ['RANK'] = '0'
    os.environ['WORLD_SIZE'] = '1'
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '5678'
    rank = int(os.environ['LOCAL_RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    gpu = int(args.gpu[rank])
    torch.cuda.set_device(gpu)
    dist.init_process_group(backend='nccl')

    set_seed(args.seed)

    os.makedirs(config.exp_dir, exist_ok=True)
    logger = get_logger('%s/train.log' % config.exp_dir)
    logger.info(f"Use GPU: {gpu} for training.")

    # build dataset
    train_dataset = build('dataset', config)
    # build dataloader
    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    config.dataloader['args']['sampler'] = train_sampler
    config.dataloader['args']['batch_size'] = int(config.batch_size / world_size)
    train_dataloader = build('dataloader', config)

    # build model
    embedding_model = build('embedding_model', config)
    if hasattr(config, 'speed_pertub') and config.speed_pertub:
        config.num_classes = len(config.label_encoder)*3
    classifier = build('classifier', config)
    model = nn.Sequential(embedding_model, classifier)
    model.cuda()
    model = torch.nn.parallel.DistributedDataParallel(model)

    # build optimizer
    config.optimizer['args']['params'] = model.parameters()
    optimizer = build('optimizer', config)

    # define loss function
    criterion = build('loss', config)

    # define scheduler
    config.lr_scheduler['args']['step_per_epoch'] = len(train_dataloader)
    lr_scheduler = build('lr_scheduler', config)
    config.margin_scheduler['args']['step_per_epoch'] = len(train_dataloader)
    margin_scheduler = build('margin_scheduler', config)

    # others
    epoch_counter = build('epoch_counter', config)
    checkpointer = build('checkpointer', config)

    epoch_logger = EpochLogger(save_file=os.path.join(config.exp_dir, 'train_epoch.log'))

    if rank == 0:
        if config.testInTrain:
            test_class = CosineTestIntraEpoch(save_dir=config.exp_dir)

    # resume from a checkpoint
    if args.resume:
        checkpointer.recover_if_possible(device='cuda')

    cudnn.benchmark = True

    for epoch in epoch_counter:
        train_sampler.set_epoch(epoch)

        # train one epoch
        train_stats = train(
            train_dataloader,
            model,
            criterion,
            optimizer,
            epoch,
            lr_scheduler,
            margin_scheduler,
            logger,
            config,
            rank,
        )

        if rank == 0:
            # log
            epoch_logger.log_stats(
                stats_meta={"epoch": epoch},
                stats=train_stats,
            )
            # save checkpoint
            if epoch % config.save_epoch_freq == 0:
                checkpointer.save_checkpoint()

            # test
            if epoch >= 40 and epoch % 5 == 0:
                if config.testInTrain:
                    test_class.test_once(embedding_model, epoch)

        dist.barrier()

def train(train_loader, model, criterion, optimizer, epoch, lr_scheduler, margin_scheduler, logger, config, rank):
    train_stats = AverageMeters()
    train_stats.add('Time', ':6.3f')
    train_stats.add('Data', ':6.3f')
    train_stats.add('Loss', ':.4e')
    train_stats.add('Acc@1', ':6.2f')
    train_stats.add('Lr', ':.3e')
    train_stats.add('Margin', ':.3f')
    progress = ProgressMeter(
        len(train_loader),
        train_stats,
        prefix="Epoch: [{}]".format(epoch)
    )

    #train mode
    model.train()

    end = time.time()
    for i, (x, y) in enumerate(train_loader):
        # x: (B, T, C)
        # y: (B, 1)

        # data loading time
        train_stats.update('Data', time.time() - end)

        # update
        iter_num = (epoch-1)*len(train_loader) + i
        lr_scheduler.step(iter_num)
        margin_scheduler.step(iter_num)

        x = x.cuda(non_blocking=True)
        y = y.cuda(non_blocking=True)

        # compute output
        output = model(x)
        loss = criterion(output, y)
        acc1 = accuracy(output, y)

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # recording
        train_stats.update('Loss', loss.item(), x.size(0))
        train_stats.update('Acc@1', acc1.item(), x.size(0))
        train_stats.update('Lr', optimizer.param_groups[0]["lr"])
        train_stats.update('Margin', margin_scheduler.get_margin())
        train_stats.update('Time', time.time() - end)

        if rank == 0 and i % config.log_batch_freq == 0:
            logger.info(progress.display(i))

        end = time.time()

    key_stats={
        'Avg_loss': train_stats.avg('Loss'),
        'Avg_acc': train_stats.avg('Acc@1'),
        'Lr_value': train_stats.val('Lr')
    }
    return key_stats

if __name__ == '__main__':
    main()