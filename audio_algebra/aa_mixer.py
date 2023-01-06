# AUTOGENERATED! DO NOT EDIT! File to edit: ../aa-mixer.ipynb.

# %% auto 0
__all__ = ['mseloss', 'train_new_aa', 'get_alphas_sigmas', 'get_crash_schedule', 'alpha_sigma_to_t', 'sample', 'DiffusionDVAE',
           'EmbedBlock', 'AudioAlgebra', 'get_stems_faders', 'do_mixing', 'aa_demo', 'rel_loss', 'vicreg_var_loss',
           'off_diagonal', 'vicreg_cov_loss', 'main']

# %% ../aa-mixer.ipynb 4
from prefigure.prefigure import get_all_args, push_wandb_config
from copy import deepcopy
import math
import json
import subprocess
import os, sys
import random
from IPython.display import display, Image, Audio, HTML
import matplotlib.pyplot as plt
import numpy as np

import accelerate
import torch
import torchaudio
from torch import optim, nn, Tensor
from torch import multiprocessing as mp
from torch.nn import functional as F
from torch.utils import data as torchdata
from tqdm import tqdm, trange
from einops import rearrange, repeat

import wandb

from aeiou.viz import embeddings_table, pca_point_cloud, show_pca_point_cloud, audio_spectrogram_image, tokens_spectrogram_image, playable_spectrogram
from aeiou.hpc import load, save, HostPrinter, freeze
from aeiou.datasets import AudioDataset

# audio-diffusion imports
import pytorch_lightning as pl
from diffusion.pqmf import CachedPQMF as PQMF
from encoders.encoders import AttnResEncoder1D
from autoencoders.soundstream import SoundStreamXLEncoder
from dvae.residual_memcodes import ResidualMemcodes
from decoders.diffusion_decoder import DiffusionAttnUnet1D
from diffusion.model import ema_update

# %% ../aa-mixer.ipynb 14
#audio-diffusion stuff 
# Define the noise schedule and sampling loop
def get_alphas_sigmas(t):
    """Returns the scaling factors for the clean image (alpha) and for the
    noise (sigma), given a timestep."""
    return torch.cos(t * math.pi / 2), torch.sin(t * math.pi / 2)

def get_crash_schedule(t):
    sigma = torch.sin(t * math.pi / 2) ** 2
    alpha = (1 - sigma ** 2) ** 0.5
    return alpha_sigma_to_t(alpha, sigma)

def alpha_sigma_to_t(alpha, sigma):
    """Returns a timestep, given the scaling factors for the clean image and for
    the noise."""
    return torch.atan2(sigma, alpha) / math.pi * 2

@torch.no_grad()
def sample(model, x, steps, eta, logits):
    """Draws samples from a model given starting noise."""
    ts = x.new_ones([x.shape[0]])

    # Create the noise schedule
    t = torch.linspace(1, 0, steps + 1)[:-1]

    t = get_crash_schedule(t)
    
    alphas, sigmas = get_alphas_sigmas(t)

    # The sampling loop
    for i in trange(steps):

        # Get the model output (v, the predicted velocity)
        with torch.cuda.amp.autocast():
            v = model(x, ts * t[i], logits).float()

        # Predict the noise and the denoised image
        pred = x * alphas[i] - v * sigmas[i]
        eps = x * sigmas[i] + v * alphas[i]

        # If we are not on the last timestep, compute the noisy image for the
        # next timestep.
        if i < steps - 1:
            # If eta > 0, adjust the scaling factor for the predicted noise
            # downward according to the amount of additional noise to add
            ddim_sigma = eta * (sigmas[i + 1]**2 / sigmas[i]**2).sqrt() * \
                (1 - alphas[i]**2 / alphas[i + 1]**2).sqrt()
            adjusted_sigma = (sigmas[i + 1]**2 - ddim_sigma**2).sqrt()

            # Recombine the predicted noise and predicted denoised image in the
            # correct proportions for the next step
            x = pred * alphas[i + 1] + eps * adjusted_sigma

            # Add the correct amount of fresh noise
            if eta:
                x += torch.randn_like(x) * ddim_sigma

    # If we are on the last timestep, output the denoised image
    return pred



class DiffusionDVAE(pl.LightningModule):
    def __init__(self, global_args):
        super().__init__()

        self.pqmf_bands = global_args.pqmf_bands

        if self.pqmf_bands > 1:
            self.pqmf = PQMF(2, 70, global_args.pqmf_bands)

        capacity = 32

        c_mults = [2, 4, 8, 16, 32]
        
        strides = [4, 4, 2, 2, 2]

        self.encoder = SoundStreamXLEncoder(
            in_channels=2*global_args.pqmf_bands, 
            capacity=capacity, 
            latent_dim=global_args.latent_dim,
            c_mults = c_mults,
            strides = strides
        )
        self.encoder_ema = deepcopy(self.encoder)

        self.diffusion = DiffusionAttnUnet1D(
            io_channels=2, 
            cond_dim = global_args.latent_dim, 
            pqmf_bands = global_args.pqmf_bands, 
            n_attn_layers=4, 
            c_mults=[256, 256]+[512]*12
        )

        self.diffusion_ema = deepcopy(self.diffusion)
        self.rng = torch.quasirandom.SobolEngine(1, scramble=True)
        self.ema_decay = global_args.ema_decay
        
        self.num_quantizers = global_args.num_quantizers
        if self.num_quantizers > 0:
            quantizer_class = ResidualMemcodes if global_args.num_quantizers > 1 else Memcodes
            
            quantizer_kwargs = {}
            if global_args.num_quantizers > 1:
                quantizer_kwargs["num_quantizers"] = global_args.num_quantizers

            self.quantizer = quantizer_class(
                dim=global_args.latent_dim,
                heads=global_args.num_heads,
                num_codes=global_args.codebook_size,
                temperature=1.,
                **quantizer_kwargs
            )

            self.quantizer_ema = deepcopy(self.quantizer)
            
        self.demo_reals_shape = None #overwrite thie later

    def encode(self, *args, **kwargs):
        if self.training:
            return self.encoder(*args, **kwargs)
        return self.encoder_ema(*args, **kwargs)

    def decode(self, *args, **kwargs):
        if self.training:
            return self.diffusion(*args, **kwargs)
        return self.diffusion_ema(*args, **kwargs)
    
    def encode_it(self, demo_reals):
        encoder_input = demo_reals

        if self.pqmf_bands > 1:
            encoder_input = self.pqmf(demo_reals)

        encoder_input = encoder_input.to(self.device)
        self.demo_reals_shape = demo_reals.shape
        
        # noise is only used for decoding tbh!
        #noise = torch.randn([demo_reals.shape[0], 2, self.demo_samples]).to(self.device)

        with torch.no_grad():
            embeddings = self.encoder_ema(encoder_input)
            if self.quantized:
                embeddings = rearrange(embeddings, 'b d n -> b n d') # Rearrange for Memcodes
                embeddings, _= self.quantizer_ema(embeddings)
                embeddings = rearrange(embeddings, 'b n d -> b d n')

            embeddings = torch.tanh(embeddings)
            return embeddings#, noise
        
    def decode_it(self, embeddings, demo_batch_size=None, demo_steps=35):
        if None==demo_batch_size: demo_batch_size = self.demo_reals_shape[0]
        noise = torch.randn([self.demo_reals_shape[0], 2, self.demo_samples]).to(self.device)
        fake_batches = sample(self.diffusion_ema, noise, demo_steps, 0, embeddings)
        audio_out = rearrange(fake_batches, 'b d n -> d (b n)') # Put the demos together
        return audio_out

# %% ../aa-mixer.ipynb 19
class EmbedBlock(nn.Module):
    def __init__(self, in_dims:int, out_dims:int, act=nn.GELU(), resid=True, use_bn=False, requires_grad=True, **kwargs) -> None:
        "generic little block for embedding stuff.  note residual-or-not doesn't seem to make a huge difference for a-a"
        super().__init__()
        self.in_dims, self.out_dims, self.act, self.resid = in_dims, out_dims, act, resid
        self.lin = nn.Linear(in_dims, out_dims, **kwargs)
        self.bn = nn.BatchNorm1d(out_dims) if use_bn else None # even though points in 2d, only one non-batch dim in data

        if requires_grad == False:
            self.lin.weight.requires_grad = False
            self.lin.bias.requires_grad = False

    def forward(self, xin: Tensor) -> Tensor:
        x = self.lin(xin)
        if self.act is not None: x = self.act(x)
        if self.bn is not None: x = self.bn(x)   # re. "BN before or after Activation? cf. https://github.com/ducha-aiki/caffenet-benchmark/blob/master/batchnorm.md"
        return xin + x if (self.resid and self.in_dims==self.out_dims) else x 

# %% ../aa-mixer.ipynb 20
class AudioAlgebra(nn.Module):
    """
    Main AudioAlgebra model. Contrast to aa-mixer code, keep this one simple & move mixing stuff outside
    """
    def __init__(self, 
                 dims=32, 
                 hidden_dims=64, 
                 act=nn.GELU(), 
                 use_bn=False, 
                 resid=True, 
                 block=EmbedBlock, 
                 trivial=False,   # ignore everything and make this an identity mapping
                ):
        super().__init__()
        self.resid, self.trivial = resid, trivial
        self.encoder = nn.Sequential(  
            block( dims,        hidden_dims, act=act,  use_bn=use_bn, resid=resid),
            block( hidden_dims, hidden_dims, act=act,  use_bn=use_bn, resid=resid),
            block( hidden_dims, hidden_dims, act=act,  use_bn=use_bn, resid=resid),
            block( hidden_dims, dims,        act=None, use_bn=use_bn, resid=resid),
        )
        self.decoder = nn.Sequential(  # same as encoder, in fact. 
            block( dims,        hidden_dims, act=act,  use_bn=use_bn, resid=resid),
            block( hidden_dims, hidden_dims, act=act,  use_bn=use_bn, resid=resid),
            block( hidden_dims, hidden_dims, act=act,  use_bn=use_bn, resid=resid),   
            block( hidden_dims, dims,        act=None, use_bn=use_bn, resid=resid),
        )
            
    def encode(self,xin):
        if self.trivial: return xin 
        x = self.encoder(xin.transpose(1,2)).transpose(1,2) # transpose is just so embeddings dim goes last for matrix mult
        return x + xin if self.resid else x

    def decode(self,xin):
        if self.trivial: return xin 
        x = self.decoder(xin.transpose(1,2)).transpose(1,2)
        return x + xin if self.resid else x

    def forward(self, 
        x   # the embedding vector from the given encoder
        ):
        xprime = self.encode(x)
        xprimeprime = self.decode(xprime)  # train system to invert itself (and hope it doesn't all collapse to nothing!)
        return xprime, xprimeprime  # encoder output,  decoder output

# %% ../aa-mixer.ipynb 21
def get_stems_faders(batch, #  "1 stem" (or batch thereof) already drawn fron the dataloader (val or train)
                     dl_iter,  # pre-made the iterator for the/a dataloader
                     dl,       # the dataloader itself, for restarting
                     maxstems=6,  # how many total stems will be used, i.e. draw maxstems-1 new stems from dl_iter
                     unity_gain=False,  # this will force all faders to be +/-1 instead of random numers
                     debug=False):
    "grab some more inputs and multiplies and some gain values to go with them"
    nstems = random.randint(2, maxstems)
    if debug: print("maxstems, nstems =",maxstems, nstems)
    device=batch.device
    faders = torch.sgn(2*torch.rand(nstems)-1)  # random +/- 1's
    if not unity_gain:
        faders += 0.5*torch.tanh(2*(2*torch.rand(nstems)-1))  # gain is now between 0.5 and 1.5
    stems = [batch]                  # note that stems is a list
    for i in range(nstems-1):        # in addtion to the batch of stem passed in, grab some more
        try: 
            next_stem = next(dl_iter).to(device)    # this is just another batch of input data
        except StopIteration:
            dl_iter = iter(dl)       # time to restart. hoping this propagates out as a pointer
            next_stem = next(dl_iter).to(device)
        if debug: print("  next_stem.shape = ",next_stem.shape)
        stems.append(next_stem)
    return stems, faders.to(device), dl_iter  # also return the iterator

# %% ../aa-mixer.ipynb 25
def do_mixing(stems, faders, given_model, aa_model, device, debug=False, **kwargs):
    """
    here we actually mix inputs and encode them and embed them.
    """
    zs, ys, zsum, ysum, yrecon_sum, fadedstems, yrecons = [], [], None, None, None, [], []
    mix = torch.zeros_like(stems[0]).to(device)
    #if debug: print("do_mixing: stems, faders =",stems, faders)
    for s, f in zip(stems, faders):   # iterate through list of stems, encode a bunch of stems at different fader settings
        fadedstem = (s * f).to(device)                 # audio stem adjusted by gain fader f
        with torch.no_grad():
            y = given_model.encode(fadedstem)  # encode the stem
        z, y_recon = aa_model(y)             # <-- this is the main work of the model
        zsum = z if zsum is None else zsum + z # <---- compute the sum of all the z's so far. we'll end up using this in our (metric) loss as "pred"

        mix += fadedstem                 # make full mix in input space
        with torch.no_grad():
            ymix = given_model.encode(mix)  # encode the mix in the given model
        zmix, ymix_recon = aa_model(ymix)   #  <----- map that according to our learned re-embedding. this will be the "target" in the metric loss

        #[y, ymix, y_recon, ymix_recon ] = [rearrange(x, 'b t e -> b e t') for x in [y, ymix, y_recon, ymix_recon ]] # put the y's back in their original order

        # Sums of y are likely meaningless but one might wonder how well the given encoder does at linearity, so...
        ysum = y if ysum is None else ysum + y   # = sum of embeddings in original model space; we don't really care about ysum except for diagnostics
        #yrecon_sum = y_recon if yrecon_sum is None else yrecon_sum + y_recon   # = sum of embeddings in original model space; we don't really care about ysum except for diagnostics

        yrecons.append(y_recon)   # for recon loss, save individual stem inverses
        zs.append(z)              # save a list of individual z's
        ys.append(y)            # save a list of individual y's
        fadedstems.append(fadedstem) # safe a list of each thing that went into the mix
        
    archive = {'zs':zs, 'mix':mix,'ys': ys, 'ymix':ymix, 'ymix_recon':ymix_recon, 'fadedstems':fadedstems, 'yrecons':yrecons, 'ysum':ysum} 

    return zsum, zmix, archive  # we will try to get these two to be close to each other via loss. archive is for diagnostics

# %% ../aa-mixer.ipynb 34
def aa_demo(given_model, aa_model, log_dict, zsum, zmix, step, demo_steps=35, sr=48000):
    "log decoded audio for zsum and zmix"
    for var,name in zip([zsum, zmix],['zsum','zmix']):
        var = aa_model.decode(var)
        fake_audio = given_model.decode_it(var, demo_steps=demo_steps)
        filename = f'{name}_{step:08}.wav'
        fake_audio = fake_audio.clamp(-1, 1).mul(32767).to(torch.int16).cpu()
        torchaudio.save(filename, fake_audio, self.sample_rate)
        log_dict[name] = wandb.Audio(filename, sample_rate=sr, caption=name)   
        #log_dict[f'{name}_spec'] = wandb.Image( tokens_spectrogram_image(var.detach()) )
    return log_dict

# %% ../aa-mixer.ipynb 36
mseloss = nn.MSELoss()

def rel_loss(y_pred: torch.Tensor, y: torch.Tensor, eps=1e-3) -> float:
    "relative error loss   --- note we're never going to actually use this. it was just part of development"
    e = torch.abs(y.view_as(y_pred) - y_pred) / ( torch.abs(y.view_as(y_pred)) + eps ) 
    return torch.median(e)

def vicreg_var_loss(z, gamma=1, eps=1e-4):
    std_z = torch.sqrt(z.var(dim=0) + eps)
    return torch.mean(F.relu(gamma - std_z))   # the relu gets us the max(0, ...)

#| export
def off_diagonal(x):
    n, m = x.shape
    assert n == m
    return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()

def vicreg_cov_loss(z):
    "the regularization term that is the sum of the off-diagaonal terms of the covariance matrix"
    cov_z = torch.cov(rearrange(z, 'b c t -> ( c t ) b'))   
    return off_diagonal(cov_z).pow_(2).sum().div(z.shape[1])

# %% ../aa-mixer.ipynb 39
train_new_aa = True  # set to false to disable this training

if train_new_aa:
    "packaging as def to re-use later.  this will use global variables, not sorry"

    max_epochs = 40
    lossinfo_every, viz_demo_every =   20, 1000   # in units of steps
    checkpoint_every = 10000
    max_lr= 0.001
    total_steps = len(train_set)//args.batch_size * max_epochs
    print("total_steps =",total_steps)  # for when I'm checking wandb

    hprint(f"Setting up AA model using device: {device}")
    #aa_model = AudioAlgebra(global_args, device, autoencoder, trivial=True)


    
    torch.manual_seed(seed) # chose this value because it shows of nice nonlinearity
    aa_model = AudioAlgebra(dims=emb_dims, hidden_dims=hidden_dims, use_bn=aa_use_bn, resid=aa_use_resid).to(device) 
    opt = optim.Adam([*aa_model.parameters()], lr=5e-4)  # Adam optimizer
    scheduler = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=max_lr, total_steps=total_steps)
    aa_model, opt, train_dl, autoencoder, scheduler = accelerator.prepare(aa_model, opt, train_dl, autoencoder, scheduler)
    given_model = autoencoder 
    
    wandb.init(project='aa-mixer-vicreg')
   
    # training loop
    train_iter = iter(train_dl) # this is only for use with get_stems_faders
    epoch, step = 0, 0
    torch.manual_seed(seed) # for reproducibility
    while (epoch < max_epochs) or (max_epochs < 0):  # training loop
        with tqdm(train_dl, unit="batch", disable=not accelerator.is_main_process) as tepoch:
            for batch in tepoch:   # train
                opt.zero_grad()
                log_dict = {}
                batch = batch.to(device)

                stems, faders, train_iter = get_stems_faders(batch, train_iter, train_dl)

                # vicreg: 1. invariance
                zsum, zmix, archive = do_mixing(stems, faders, accelerator.unwrap_model(given_model), accelerator.unwrap_model(aa_model), device, debug=debug)
                mix_loss = mseloss(zsum, zmix)  

                var_loss = (vicreg_var_loss(zsum) + vicreg_var_loss(zmix))/2    # vicreg: 2. variance
                cov_loss = (vicreg_cov_loss(zsum) + vicreg_cov_loss(zmix))/2    # vicreg: 3. covariance


                # reconstruction loss: inversion of aa map h^{-1}(z): z -> y,  i.e. train the aa decoder
                y = accelerator.unwrap_model(given_model).encode(batch)
                z, yrecon = accelerator.unwrap_model(aa_model).forward(y)       # (re)compute ys for one input batch (not stems&faders)
                aa_recon_loss = mseloss(y, yrecon)     
                aa_recon_loss = aa_recon_loss + mseloss(archive['ymix'], archive['ymix_recon'])  # also recon of the  mix ecoding
                #aa_recon_loss  = aa_recon_loss +  mseloss(archive['ysum'], archive['yrecon_sum']) # Never use this:  ysum shouldn't matter / is poorly defined
           
                loss = mix_loss + var_loss + cov_loss + aa_recon_loss     # --- full loss function
                
                log_dict['train_loss'] = loss.detach()                    # --- this is the full loss 
                log_dict['mix_loss'] = mix_loss.detach() 
                log_dict['aa_recon_loss'] = aa_recon_loss.detach()
                log_dict['var_loss'] = var_loss.detach() 
                log_dict['cov_loss'] = cov_loss.detach() 
                log_dict['learning_rate'] = opt.param_groups[0]['lr']
                log_dict['epoch'] = epoch

                if step % lossinfo_every == 0: 
                    tepoch.set_description(f"Epoch {epoch+1}/{max_epochs}")
                    tepoch.set_postfix(loss=loss.item())         #  TODO: use EMA for loss display? 

                #loss.backward()
                accelerator.backward(loss)
                opt.step()  
                
                if accelerator.is_main_process:
                    with torch.no_grad():
                        if step % viz_demo_every == 0:
                             log_dict = aa_demo(autoencoder, aa_model, log_dict, zsum, zmix, step)

                        # run on demo data (same each time)
                        #demo_zsum, demo_zmix, demo_archive = do_mixing(demo_stems, demo_faders, accelerator.unwrap_model(given_model), accelerator.unwrap_model(aa_model), device, debug=debug)
                        #demo_mix_loss = mseloss(demo_zsum, demo_zmix)
                        #log_dict['demo_mix_loss'] = demo_mix_loss.detach()

                        #if step % viz_demo_every == 0:   # but only draw the viz every now and then. 
                            #try:     # to avoid "OSError: image file is truncated"
                            #    im = viz_aa_demo(demo_zsum, demo_zmix, demo_archive, accelerator.unwrap_model(aa_model))
                            #    log_dict["aa_mapping"] = wandb.Image(im)
                            #except:
                            #    pass
                        if step % viz_demo_every == 0:
                             log_dict = aa_demo(accelerator.unwrap_model(autoencoder), log_dict, zsum, zmix, step)

                    if step % checkpoint_every == 0:
                        save_aa_checkpoint(aa_model, suffix=RUN_SUFFIX+f"_{step}")

                    wandb.log(log_dict)

                scheduler.step()   
                step += 1

        epoch += 1
    #----- training loop finished
    
    save_aa_checkpoint(accelerator.unwrap_model(aa_model), suffix=RUN_SUFFIX+f"_{step}")
    

# %% ../aa-mixer.ipynb 42
def main():

    args = get_all_args()
    torch.manual_seed(args.seed)

    try:
        mp.set_start_method(args.start_method)
    except RuntimeError:
        pass

    accelerator = accelerate.Accelerator()
    device = accelerator.device
    hprint = HostPrinter(accelerator)
    hprint(f'Using device: {device}')

    encoder_choices = ['ad','icebox']
    encoder_choice = encoder_choices[0]
    hprint(f"Using {encoder_choice} as encoder")
    if 'icebox' == encoder_choice:
        args.latent_dim = 64  # overwrite latent_dim with what Jukebox requires
        encoder = IceBoxModel(args, device)
    elif 'ad' == encoder_choice:
        dvae = DiffusionDVAE(args, device)
        #dvae = setup_weights(dvae, accelerator, device)
        #encoder = dvae.encoder
        #freeze(dvae)

    hprint("Setting up AA model")
    aa_model = AudioAlgebra(args, device, dvae)

    hprint(f'  AA Model Parameters: {n_params(aa_model)}')

    # If logging to wandb, initialize the run
    use_wandb = accelerator.is_main_process and args.name
    if use_wandb:
        import wandb
        config = vars(args)
        config['params'] = n_params(aa_model)
        wandb.init(project=args.name, config=config, save_code=True)

    opt = optim.Adam([*aa_model.reembedding.parameters()], lr=4e-5)

    hprint("Setting up dataset")
    train_set = MultiStemDataset([args.training_dir], args)
    train_dl = torchdata.DataLoader(train_set, args.batch_size, shuffle=True,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True)

    hprint("Calling accelerator.prepare")
    aa_model, opt, train_dl, dvae = accelerator.prepare(aa_model, opt, train_dl, dvae)

    hprint("Setting up frozen encoder model weights")
    dvae = setup_weights(dvae, accelerator)
    freeze(accelerator.unwrap_model(dvae))
    #encoder = dvae.encoder 

    hprint("Setting up wandb")
    if use_wandb:
        wandb.watch(aa_model)

    hprint("Checking for checkpoint")
    if args.ckpt_path:
        ckpt = torch.load(args.ckpt_path, map_location='cpu')
        accelerator.unwrap_model(aa_model).load_state_dict(ckpt['model'])
        opt.load_state_dict(ckpt['opt'])
        epoch = ckpt['epoch'] + 1
        step = ckpt['step'] + 1
        del ckpt
    else:
        epoch = 0
        step = 0

    # all set up, let's go
    hprint("Let's go...")
    try:
        while True:  # training loop
            #print(f"Starting epoch {epoch}")
            for batch in tqdm(train_dl, disable=not accelerator.is_main_process):
                batch = batch[0]       # first elem is the audio, 2nd is the filename which we don't need
                #hprint(f"e{epoch} s{step}: got batch. batch.shape = {batch.shape}")
                opt.zero_grad()

                # "batch" is actually not going to have all the data we want. We could rewrite the dataloader to fix this,
                # but instead I just added get_stems_faders() which grabs "even more" audio to go with "batch"
                stems, faders = get_stems_faders(batch, train_dl)

                zsum, zmix, zarchive = accelerator.unwrap_model(aa_model).forward(stems,faders)  # Here's the model's .forward
                loss = accelerator.unwrap_model(aa_model).loss(zsum, zmix, zarchive)
                accelerator.backward(loss)
                opt.step()

                if accelerator.is_main_process:
                    if step % 25 == 0:
                        tqdm.write(f'Epoch: {epoch}, step: {step}, loss: {loss.item():g}')

                    if use_wandb:
                        log_dict = {
                            'epoch': epoch,
                            'loss': loss.item(),
                            #'lr': sched.get_last_lr()[0],
                            'zsum_pca': pca_point_cloud(zsum.detach()),
                            'zmix_pca': pca_point_cloud(zmix.detach())
                        }

                        if (step % args.demo_every == 0):                                                    
                            hprint("\nMaking demo stuff")

                            mix_filename = f'mix_{step:08}.wav'
                            reals = zarchive['mix'].clamp(-1, 1).mul(32767).to(torch.int16).cpu()
                            reals = rearrange(reals, 'b d n -> d (b n)')
                            print("reals.shape = ",reals.shape)
                            torchaudio.save(mix_filename, reals, args.sample_rate)
                            log_dict['mix'] = wandb.Audio(mix_filename, sample_rate=args.sample_rate, caption='mix')

                            #demo(accelerator.unwrap_model(dvae), log_dict, zsum.detach(), zmix.detach(),  batch.shape[-1], step)
                            zsum = zarchive['z0sum'].detach() # rearrange(zarchive['z0sum'], 'b n d -> b d n').detach()
                            zmix = zarchive['z0mix'].detach() #rearrange(zarchive['z0mix'], 'b n d -> b d n').detach()

                            hprint(f"zsum.shape = {zsum.shape}")
                            noise = torch.randn([zsum.shape[0], 2, batch.shape[-1]]).to(accelerator.device)
                            accelerator.unwrap_model(dvae).diffusion_ema.to(accelerator.device)
                            model_fn = make_cond_model_fn(accelerator.unwrap_model(dvae).diffusion_ema, zsum)
                            hprint(f"noise.shape = {noise.shape}")

                            # Run the sampler
                            with torch.cuda.amp.autocast():
                                hprint("Calling sampler for zsum")
                                fakes = sample(accelerator.unwrap_model(dvae).diffusion_ema, noise, args.demo_steps, 1, zsum)
                            fakes = rearrange(fakes, 'b d n -> d (b n)')
                            zsum_filename = f'zsum_{step:08}.wav'
                            fakes = fakes.clamp(-1, 1).mul(32767).to(torch.int16).cpu()
                            torchaudio.save(zsum_filename, fakes, args.sample_rate)
                            log_dict['zsum'] = wandb.Audio(zsum_filename, sample_rate=args.sample_rate, caption='zsum')
                            
                            with torch.cuda.amp.autocast():
                                hprint("Calling sampler for zmix")
                                fakes = sample(accelerator.unwrap_model(dvae).diffusion_ema, noise, args.demo_steps, 1, zmix)
                            fakes = rearrange(fakes, 'b d n -> d (b n)')
                            zmix_filename = f'zmix_{step:08}.wav'
                            fakes = fakes.clamp(-1, 1).mul(32767).to(torch.int16).cpu()
                            torchaudio.save(zmix_filename, fakes, args.sample_rate)
                            log_dict['zmix'] = wandb.Audio(zmix_filename, sample_rate=args.sample_rate, caption='zmix')
                            hprint("Done making demo stuff")
                            
                    if use_wandb: wandb.log(log_dict, step=step)

                if step > 0 and step % args.checkpoint_every == 0:
                    save(accelerator, args, aa_model, opt, epoch, step)

                step += 1
            epoch += 1
    except RuntimeError as err:  # ??
        import requests
        import datetime
        ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        resp = requests.get('http://169.254.169.254/latest/meta-data/instance-id')
        hprint(f'ERROR at {ts} on {resp.text} {device}: {type(err).__name__}: {err}', flush=True)
        raise err
    except KeyboardInterrupt:
        pass

# %% ../aa-mixer.ipynb 43
# Not needed if listed in console_scripts in settings.ini
if __name__ == '__main__' and "get_ipython" not in dir():  # don't execute in notebook
    main() 
