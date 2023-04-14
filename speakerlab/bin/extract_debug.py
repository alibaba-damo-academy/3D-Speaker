import os
import sys
sys.path.append('/mnt/workspace/gitlab/3D-Speaker')
os.chdir('/mnt/workspace/gitlab/3D-Speaker/egs/voxceleb-sv/v1')
import argparse
import torch
import torchaudio
from kaldiio import WriteHelper

from speakerlab.utils.builder import build
from speakerlab.utils.utils import get_logger
from speakerlab.utils.config import build_config
from speakerlab.utils.fileio import load_wav_scp

parser = argparse.ArgumentParser(description='Extract embeddings for evaluation.')
parser.add_argument('--exp_dir', default='', type=str, help='Exp dir')
parser.add_argument('--data', default='', type=str, help='Data dir')
parser.add_argument('--use_gpu', action='store_true', help='Use gpu or not')
parser.add_argument('--gpu', nargs='+', help='GPU id to use.')


def main():
    args_list = ['--exp_dir', 'exp/cam++_1_meanbad', '--data', 'data/vox1/wav.scp', '--gpu', '0','1','2','3']
    args = parser.parse_args(args_list)
    config_file = os.path.join(args.exp_dir, 'config.yaml')
    config = build_config(config_file)

    os.environ['LOCAL_RANK'] = '0'
    os.environ['RANK'] = '0'
    os.environ['WORLD_SIZE'] = '1'
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '5678'

    rank = int(os.environ['LOCAL_RANK'])
    world_size = int(os.environ['WORLD_SIZE'])

    embedding_dir = os.path.join(args.exp_dir, 'embeddings')
    os.makedirs(embedding_dir, exist_ok=True)

    logger = get_logger()

    if args.use_gpu:
        if torch.cuda.is_available():
            gpu = int(args.gpu[rank % len(args.gpu)])
            device = torch.device('cuda', gpu)
        else:
            msg = 'No cuda device is detected. Using the cpu device.'
            if rank == 0:
                logger.warning(msg)
            device = torch.device('cpu')
    else:
        device = torch.device('cpu')

    # Build the embedding model
    embedding_model = build('embedding_model', config)

    # Recover the embedding params of last epoch
    config.checkpointer['args']['checkpoints_dir'] = os.path.join(args.exp_dir, 'models')
    config.checkpointer['args']['recoverables'] = {'embedding_model':embedding_model}
    checkpointer = build('checkpointer', config)
    checkpointer.recover_if_possible(epoch=config.num_epoch, device=device)

    embedding_model.to(device)
    embedding_model.eval()
    feature_extractor = build('feature_extractor', config)

    data = load_wav_scp(args.data)
    data_k = list(data.keys())
    local_k = data_k[rank::world_size]
    if len(local_k) == 0:
        msg = "The number of threads exceeded the number of files"
        logger.info(msg)
        sys.exit()

    emb_ark = os.path.join(embedding_dir, 'xvector_%02d.ark'%rank)
    emb_scp = os.path.join(embedding_dir, 'xvector_%02d.scp'%rank)

    if rank == 0:
        logger.info('Start extracting embeddings.')
    with torch.no_grad():
        with WriteHelper(f'ark,scp:{emb_ark},{emb_scp}') as writer:
            for k in local_k:
                wav_path = data[k]
                wav, fs = torchaudio.load(wav_path)
                assert fs == config.sample_rate, f"The sample rate of wav is {fs} and inconsistent with that of the pretrained model."
                feat = feature_extractor(wav)
                feat = feat.unsqueeze(0)
                feat = feat.to(device)
                emb = embedding_model(feat).detach().cpu().numpy()
                writer(k, emb)

if __name__ == "__main__":
    main()