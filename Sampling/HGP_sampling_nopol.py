#first import dependencies, mainly jax and numpyro
import time

#we change the config to use 
from jax.config import config
config.update("jax_enable_x64", True)

import matplotlib.pyplot as plt
import numpy as np
import arviz as az

from jax import vmap
import jax.numpy as jnp
import jax.random as random

import numpyro
import numpyro.distributions as dist
from numpyro.infer import (
    MCMC,
    NUTS,
    init_to_feasible,
    init_to_median,
    init_to_sample,
    init_to_uniform,
    init_to_value,
)
import pandas as pd
#read the data, please change to your data directory
data = pd.read_pickle('/home/dkn16/data.pkl')
FGnopol = data.beam.foregrounds.all_nopol
FGpol = data.beam.foregrounds.pleak
HI = data.beam.HI
noise = data.beam.noise
freqs = data.freqs

superpixel = 16

# read and resize our data
def get_data(dim,pol=False,x0=0,y0=0,freqs = 285,superpixel = 1,selected = None):
    #dim,freqs: the datasize is (dim,dim,freqs)
    #pol(bool): if True, include the polarization leakage data
    #x0,y0: starting coordinates. e.g. pixels within x0:x0+dim will be included in the data.
    #superpixel: if larger than one, reshape the data to be (x_sp,y_sp,x_withinsp,y_withinsp,freq). In this case, data[0][0] is all pixels within superpixel (0,0)
    #selected: indexes to select several channels.
    
    #foreground
    if pol:
        sky = jnp.array(FGnopol+FGpol)[x0:x0+dim,y0:y0+dim,0:freqs].astype(jnp.float64)
    else:
        sky = jnp.array(FGnopol)[x0:x0+dim,y0:y0+dim,0:freqs].astype(jnp.float64)
    
    #HI signal
    cosmos = jnp.array(HI+noise)[x0:x0+dim,y0:y0+dim,0:freqs].astype(jnp.float64)
    
    #substract mean to 
    sky = sky - jnp.mean(sky,axis=(0,1))
    cosmos = cosmos - jnp.mean(cosmos,axis=(0,1))
    
    sky=sky.reshape((dim*dim,freqs))/1000
    cosmos=cosmos.reshape((dim*dim,freqs))/1000
    
    X = jnp.linspace(0., 1., sky.shape[1]).astype(jnp.float64)
    
    Y = sky+cosmos


    if superpixel >1:
        Y = Y.reshape((dim,dim,freqs))
        Y = Y.reshape((int(dim/superpixel),superpixel,int(dim/superpixel),superpixel,freqs)).transpose((0,2,1,3,4))
        Y = Y.reshape((int(int(dim/superpixel)**2),-1,freqs)).transpose((1,0,2))

    
    if selected is not None:
        X = X[selected]
        Y = Y[:,:,selected]

    return X, Y

# RBF kernel for fg, exponential kernel for HI, and a diagonal noise kernel
def kernel(X, Z, var, length,var_HI,length_HI, noise, jitter=1.0e-16,is_noise=True):
    deltaXsq = jnp.power((X[:, None] - Z) / length, 2.0)
    deltaHI = jnp.abs((X[:, None] - Z) / length_HI)
    
    k_fg = 1.0e-2*var * jnp.exp(-0.5 * deltaXsq)
    k_HI = 1.0e-9*var_HI * jnp.exp(-0.5 * deltaHI)
    #k = var * jnp.exp(-0.5 * deltaXsq)
    if is_noise:
        k_HI += (noise*noise*1.0e-14 + jitter) * jnp.eye(X.shape[0])
        k_fg += k_HI
    return k_fg

# RBF kernel for fg, another RBF kernel for pol, exponential kernel for HI, and a diagonal noise kernel
def kernel_pol(X, Z, var, length,var_pol,length_pol,var_HI,length_HI, noise, jitter=1.0e-16,is_noise=True):
    deltaXsq = jnp.power((X[:, None] - Z) / length, 2.0)
    deltapol = jnp.power((X[:, None] - Z) / length_pol, 2.0)
    deltaHI = jnp.abs((X[:, None] - Z) *100/ length_HI)
    
    k_fg = 1.0e-2*var * jnp.exp(-0.5 * deltaXsq)
    k_pol = 1.0e-6*var_pol * jnp.exp(-0.5 * deltapol)
    k_HI = 1.0e-9*var_HI * jnp.exp(-0.5 * deltaHI)
    #k = var * jnp.exp(-0.5 * deltaXsq)
    if is_noise:
        k_HI += (noise*noise*1.0e-14 + jitter) * jnp.eye(X.shape[0])
        k_fg += k_HI
    return k_fg + k_pol

def model(X, Y):
    # set weakly-informative log-normal priors on our three kernel hyperparameters
    var_fg_std = numpyro.sample("varfg_std", dist.LogNormal(0,4))
    
    length_fg_alpha = numpyro.sample("length_fg_alpha",dist.LogNormal(1,4))
    length_fg_beta = numpyro.sample("length_fg_beta",dist.LogNormal(0,4))

    noise = numpyro.sample("kernel_noise", dist.HalfNormal(10))
    
    #var_pol_std = numpyro.sample("varpol_std", dist.HalfNormal(5))
    
    #length_pol_mean = numpyro.sample("length_pol_mean",dist.Normal(0.25,1))
    #length_pol_std = numpyro.sample("length_pol_std",dist.HalfNormal(0.5))
    
    var_HI = numpyro.sample("kernel_varHI", dist.HalfNormal(1))
    length_HI = numpyro.sample("kernel_lengthHI",dist.HalfNormal(2))
    var_fg = numpyro.sample("kernel_var", dist.HalfNormal(jnp.ones(Y.shape[1])*var_fg_std))
    length_fg = numpyro.sample("kernel_length", dist.InverseGamma(jnp.ones(Y.shape[1])*length_fg_alpha,jnp.ones(Y.shape[1])*length_fg_beta))
    #var_pol = numpyro.sample("kernel_varpol", dist.HalfNormal(jnp.ones(Y.shape[1])*var_pol_std))
    #length_pol = numpyro.sample("kernel_lengthpol", dist.Normal(jnp.ones(Y.shape[1])*length_pol_mean,jnp.ones(Y.shape[1])*length_pol_std))

    # compute kernel
    X=jnp.repeat(jnp.array([X]),Y.shape[1],axis=0)

    vmap_args = (
        X,X,var_fg,length_fg#,var_pol,length_pol
    )
    
    k = vmap(
        lambda X,  Z,var_fg,length_fg: kernel(
            X, Z,var_fg,length_fg,var_HI,length_HI,noise
        )
    )(*vmap_args)
    
    numpyro.sample(
        "Y",
        dist.MultivariateNormal(loc=jnp.zeros((Y.shape[1],Y.shape[2])), covariance_matrix=k),
        obs=Y,
    )


# helper function for doing hmc inference
def run_inference(model,init_strategy, rng_key, X, Y):
    start = time.time()
    # demonstrate how to use different HMC initialization strategies
    if init_strategy == "value":
        init_strategy = init_to_value(
            values={"kernel_var": jnp.ones(Y.shape[1]), "kernel_noise": 1, "kernel_length": jnp.ones(Y.shape[1]), "kernel_varpol": jnp.ones(Y.shape[1]), "kernel_lengthpol": 0.5*jnp.ones(Y.shape[1]),"kernel_varHI": 1.0, "kernel_lengthHI": 1.0,"varfg_std":1.0,"length_fg_alpha":2.,"length_fg_beta":1.,"varpol_std":1.,"length_pol_mean":5.,"length_pol_std":1.}
        )
    elif init_strategy == "median":
        init_strategy = init_to_median(num_samples=10)
    elif init_strategy == "feasible":
        init_strategy = init_to_feasible()
    elif init_strategy == "sample":
        init_strategy = init_to_sample()
    elif init_strategy == "uniform":
        init_strategy = init_to_uniform(radius=1)

    #pass our model to numpyro built-in NUTS and MCMC function
    kernel = NUTS(
        model,
        init_strategy=init_strategy,
        target_accept_prob=0.8,
        max_tree_depth=8)
    mcmc = MCMC(
        kernel,
        num_warmup=1000,
        num_samples=2000,
        num_chains=1,
        thinning=1,
        progress_bar= True,
    )

    #mcmc.run() would do everything
    mcmc.run(rng_key, X, Y)
    mcmc.print_summary()

    print("\nMCMC elapsed time:", time.time() - start)

    #here we use arviz package to calculate the cross validation score, which is equivalent to Bayesian evidence.
    idata = az.from_numpyro(mcmc)
    loo_orig = az.loo(idata, pointwise=True)
    print(loo_orig)
    return mcmc.get_samples(),mcmc




def main():
    #selected = np.concatenate([np.arange(64),np.arange(32)+128,np.arange(32)+224])
    selected = None
    X, Y = get_data(pol=True,dim=256,x0=0,y0=0,freqs=256,superpixel=superpixel,selected=selected)

    # do inference
    rng_key, rng_key_predict = random.split(random.PRNGKey(42))
    samples,mcmc = run_inference(model, "median", rng_key, X, Y)
    return samples,mcmc

numpyro.set_platform('gpu')
numpyro.set_host_device_count(1)
samples,mcmc = main()

import pickle
#pickle the whole object for post analysis
param_file = open('samples_hgp_nopol.bin', 'wb')
pickle.dump(mcmc, param_file)
param_file.close()