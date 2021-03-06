import argparse
import os
import random
import torch
import torch.backends.cudnn as cudnn
import torch.optim as optim
import re
import shutil
from IPython import embed
from pathlib import Path
from data import *
from modules import *
from utils import *
from scores import *


default_config = {
    'architecture':{
        'lda_dim': 200},
    'training': {
        'loss': 'cross_entropy',
        'ptar': 0.01,
        'max_norm': 4,
        'l2_reg': 0.0001,
        'learning_rate': 0.0005,
        'betas': (0.5, 0.99),
        'learning_rate_params': None,
        'init_params': {'w_init': 0.5},
        'batch_size': 256,
        'num_epochs': 50,
        'num_samples_per_spk': 2,
        'num_batches_per_epoch': 1000,
        'balance_batches_by_domain': False}}


def check_if_best(best_checkpoint, best_dev_loss, best_epoch, checkpoint, dev_loss, epoch):

    if dev_loss < best_dev_loss:
        best_checkpoint = checkpoint
        best_dev_loss = dev_loss
        best_epoch = epoch

    return best_checkpoint, best_epoch, best_dev_loss


def test_and_save(model, data_dict, epoch, optimizer, lr_scheduler, out_dir, trn_loss, ptar, loss_type):
    # Compute the loss for each of the devsets. The validation loss will be the average over all devsets
    av_loss = 0
    for name, info in data_dict.items():
        mask = info['mask']
        scores = evaluate(model, info['dataset'], info['emap'], info['tmap'])
        # Since the key mask was created to be aligned to the emap and tmap, the score_mat
        # is already aligned to this mask
        loss = compute_loss(scores.score_mat, mask=mask, ptar=ptar, loss_type=loss_type)
        print("Epoch %04d, loss on dev set %s = %f"%(epoch, name, loss))
        av_loss += loss

    av_loss /= len(data_dict)
    outfile = save_checkpoint(model, out_dir, epoch, trn_loss, av_loss, optimizer, lr_scheduler)
    return av_loss, outfile

def load_data_dict(table, device):
    data_dict = dict()
    for line in open(table).readlines():
        f = line.strip().split()
        name, emb, key, emapf, tmapf = f[0:5]
        dur = f[5] if len(f) > 3 else None
        dataset = SpeakerDataset(emb, dur, meta_is_dur_only=True, device=device)
        emap = IdMap.load(emapf, dataset.get_ids())
        tmap = IdMap.load(tmapf, dataset.get_ids())
        # Load key file in the order in which the model ids were loaded in emap 
        # and tmap. This ensures that the scores and keys will be aligned.
        mask = np_to_torch(Key.load(key, emap.model_ids, tmap.model_ids).mask, device)
        data_dict[name] = {'dataset': dataset, 'mask': mask, 'emap': emap, 'tmap': tmap}
    return data_dict

parser = argparse.ArgumentParser()
parser.add_argument('--debug',        help='Enable debug mode.', action='store_true')
parser.add_argument('--cuda',         help='Enable cuda.', action='store_true')
parser.add_argument('--seed',         help='Seed used for training.', default=0, type=int)
parser.add_argument('--configs',      help='List of configuration files to load. They are loaded in order, from left to right, overwriting previous values for repeated parameters.', default=None)
parser.add_argument('--mods',         help='List of values to overwride config parameters. Example: training.num_epochs=20,architecture.lda_dim=200', default=None)
parser.add_argument('--init_subset',  help='Subset of the train files to be used for initialization. For default, the files in trn_metafile are used.', default=None)
parser.add_argument('--restart',      help='Restart training from last available model.', action='store_true')
parser.add_argument('trn_embeddings', help='Path to the npz file with training embeddings.')
parser.add_argument('trn_metafile',   help='Path to the metadata for the training samples (all samples listed in this file should be present in the embeddings file).')
parser.add_argument('dev_table',      help='Path to a table with one dev set per line, including: name, npz file with embeddings, key file, and durations file (can be missing if not using duration-dependent calibration).')
parser.add_argument('out_dir',        help='Output directory for models.')

opt = parser.parse_args()
mkdirp(opt.out_dir)

##### Read the configs
config = load_configs(opt.configs, default_config, opt.mods, "%s/config"%(opt.out_dir))

##### Set the seed 
opt.seed = random.randint(1, 10000) if opt.seed is None else opt.seed
print("Using seed: ", opt.seed)
random.seed(opt.seed)
torch.manual_seed(opt.seed)

##### Set the device and default data type
cudnn.benchmark = True
if opt.cuda:
    torch.set_default_tensor_type(torch.cuda.FloatTensor)
    device = torch.device("cuda:0")
else:
    torch.set_default_tensor_type(torch.FloatTensor)
    device = torch.device("cpu")

###### Load the dataset and create the model object
trn_metafiles = opt.trn_metafile.split(",")
trn_dataset = SpeakerDataset(opt.trn_embeddings, trn_metafiles[0])
in_size = trn_dataset[0]['emb'].shape[0]
model = DCA_PLDA_Backend(in_size, config.architecture).to(device)
print_graph(model, trn_dataset, device, opt.out_dir)

dev_data_dict = load_data_dict(opt.dev_table, device)

###### Train
mkdirp(opt.out_dir)
print("\n####################################################################################")
print("Starting training")

config_trn = config.training
param_names = [k for k, v in model.named_parameters()]
config_trn['l2_reg_dict'] = expand_regexp_dict(param_names, config_trn.l2_reg)
num_epochs = config_trn.num_epochs

# Initialize the optimizer and learning rate scheduler
parameters = get_parameters_to_train(model, config_trn.get('freeze_params'))
optimizer = optim.Adam(parameters, lr=config_trn.learning_rate, betas=config_trn.betas)
lr_scheduler = None

if config_trn.get("learning_rate_params") is not None:
    conf = config_trn.learning_rate_params
    lrtype = conf['type']
    factor = conf['factor']

    if lrtype == 'CyclicLR':
        lr = config_trn.learning_rate
        lr_scheduler = optim.lr_scheduler.CyclicLR(optimizer, lr/factor, lr, cycle_momentum=False, step_size_up=conf['step_size_up'], step_size_down=conf['step_size_down'])
    else:
        patience = conf['patience']
        lr_scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=factor, patience=patience, threshold=0.001, min_lr=0.000001) 


# Load the last available model if restart is set or initialize with data if no previous checkpoint is available
last_checkpoint,  last_epoch,  last_dev_loss  = find_checkpoint(opt.out_dir)

if opt.restart and last_checkpoint is not None:

    print("Loading model from %s"%last_checkpoint)

    if last_epoch == 0:
        # We are loading the zeroth epoch which might be from a prior stage or just the initialization with data, 
        # so, do not load the optimizer's or lr_scheduler check point in this case
        load_checkpoint(last_checkpoint, model, device)
    else:           
        load_checkpoint(last_checkpoint, model, device, optimizer, lr_scheduler)

    best_checkpoint,  best_epoch,  best_dev_loss  = find_checkpoint(opt.out_dir, "best")
    first_checkpoint, first_epoch, first_dev_loss = find_checkpoint(opt.out_dir, "first")

else:

    if opt.init_subset:
        print("Initializing model using a subset of the training data: %s"%opt.init_subset)
        init_subset = [l.strip().split()[0] for l in open(opt.init_subset).readlines()]
    else:
        print("Initializing model using all training data")
        init_subset = None

    trn_loss = model.init_params_with_data(trn_dataset, config_trn, device=device, subset=init_subset)
    dev_loss, checkpoint = test_and_save(model, dev_data_dict, 0, optimizer, lr_scheduler, opt.out_dir, trn_loss, config_trn.ptar, config_trn.loss)
    best_checkpoint,  best_epoch,  best_dev_loss  = checkpoint, 0, dev_loss
    first_checkpoint, first_epoch, first_dev_loss = checkpoint, 0, dev_loss


# Create the trial loader
loader = TrialLoader(trn_dataset, device, seed=opt.seed, batch_size=config_trn.batch_size, num_batches=config_trn.num_batches_per_epoch, 
                     balance_by_domain=config_trn.balance_batches_by_domain, num_samples_per_spk=config_trn.num_samples_per_spk)

# Train the model
start_epoch = 1 if not opt.restart else last_epoch+1
for epoch in range(start_epoch, num_epochs + 1):
    trn_loss = train(model, loader, optimizer, epoch, config_trn, debug_dir=opt.out_dir if opt.debug else None)
    dev_loss, checkpoint = test_and_save(model, dev_data_dict, epoch, optimizer, lr_scheduler, opt.out_dir, trn_loss, config_trn.ptar, config_trn.loss)
    best_checkpoint, best_epoch, best_dev_loss = check_if_best(best_checkpoint, best_dev_loss, best_epoch, checkpoint, dev_loss, epoch)

    if lr_scheduler is not None:
        if lrtype == 'CyclicLR':
            lr_scheduler.step()
        else:
            lr_scheduler.step(dev_loss)

# Copy the first, best and the last models to files with explicit names 
shutil.copy(checkpoint,       '%s/last_epoch_%04d_devloss_%.4f.pth' % (opt.out_dir, epoch, dev_loss))
shutil.copy(best_checkpoint,  '%s/best_epoch_%04d_devloss_%.4f.pth' % (opt.out_dir, best_epoch, best_dev_loss))
shutil.copy(first_checkpoint, '%s/first_epoch_%04d_devloss_%.4f.pth' % (opt.out_dir, first_epoch, first_dev_loss))

Path("%s/DONE"%opt.out_dir).touch()

