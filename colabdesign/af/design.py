import random, os
import jax
import jax.numpy as jnp
import numpy as np
from colabdesign.af.alphafold.common import residue_constants
from colabdesign.shared.utils import copy_dict, update_dict, Key, dict_to_str

try:
  from jax.example_libraries.optimizers import sgd, adam
except:
  from jax.experimental.optimizers import sgd, adam

####################################################
# AF_DESIGN - design functions
####################################################
#\
# \_af_design
# |\
# | \_restart
#  \
#   \_design
#    \_step
#    |\_run
#    | \_recycle
#     \_save_results
#
####################################################

class _af_design:

  def restart(self, seed=None, optimizer="sgd", opt=None, weights=None,
              seq=None, keep_history=False, reset_opt=False, **kwargs):   
    '''restart the optimizer''' 
    
    # reset [opt]ions
    if reset_opt: self.opt = copy_dict(self._opt)

    # update options/settings (if defined)
    self.set_opt(opt)
    self.set_weights(weights)

    # initialize sequence
    self.key = Key(seed=seed).get
    self.set_seq(seq=seq, **kwargs)

    # setup optimizer
    if isinstance(optimizer, str):
      if optimizer == "adam": (optimizer,lr) = (adam,0.02)
      if optimizer == "sgd":  (optimizer,lr) = (sgd,0.1)
      self.opt["lr"] = lr
    self._init_fun, self._update_fun, self._get_params = optimizer(1.0)
    self._state = self._init_fun(self.params)
    self._k = 0

    if not keep_history:
      # initialize trajectory
      self._traj = {"losses":[],"seq":[],"xyz":[],"plddt":[],"pae":[]}
      self._best_loss = np.inf
      self._best_aux = self._best_outs = None
  
  def step(self, lr_scale=1.0, backprop=True, callback=None,
           save_best=False, verbose=1):
    '''do one step of gradient descent'''
    
    # run
    self.run(backprop=backprop)
    if callback is not None: callback(self)

    # normalize gradient
    g = self.grad["seq"]
    gn = jnp.linalg.norm(g,axis=(-1,-2),keepdims=True)
    self.grad["seq"] *= jnp.sqrt(self._len)/(gn+1e-7)

    # set learning rate
    lr = self.opt["lr"] * lr_scale
    self.grad = jax.tree_map(lambda x:x*lr, self.grad)

    # apply gradient
    self._state = self._update_fun(self._k, self.grad, self._state)
    self.params = self._get_params(self._state)

    # increment
    self._k += 1

    # save results
    self.save_results(save_best=save_best, verbose=verbose)
    
  def run(self, backprop=True):
    '''run model to get outputs, losses and gradients'''
    
    # decide which model params to use
    ns = jnp.arange(2) if self._args["use_templates"] else jnp.arange(5)
    m = min(self.opt["models"],len(ns))
    if self.opt["sample_models"] and m != len(ns):
      model_num = jax.random.choice(self.key(),ns,(m,),replace=False)
    else:
      model_num = ns[:m]
    model_num = np.array(model_num).tolist()    
    
    # loop through model params
    _loss, _aux, _grad = [],[],[]
    for n in model_num:
      p = self._model_params[n]
      if backprop:
        (l,a),g = self._recycle(p)
        _grad.append(g)
      else:
        l,a = self._recycle(p, backprop=False)
      _loss.append(l); _aux.append(a)

    # update gradients
    if backprop:
      self.grad = jax.tree_map(lambda *x: jnp.stack(x).mean(0), *_grad)
    else:
      self.grad = jax.tree_map(jnp.zeros_like, self.params)

    # update loss (take mean across models)
    self.loss = jnp.stack(_loss).mean()
    
    # update aux
    _aux = jax.tree_map(lambda *x: jnp.stack(x), *_aux)    
    self.aux = jax.tree_map(lambda x:x[0], _aux)
    self.aux["losses"] = jax.tree_map(lambda x: x.mean(0), _aux["losses"])
    self.aux["model_num"] = model_num    
    self._outs = self.aux # backward compatibility
      
  def _recycle(self, model_params, backprop=True):                
    # initialize previous (for recycle)
    L = self._inputs["residue_index"].shape[-1]
    self._inputs["prev"] = {'prev_msa_first_row': np.zeros([L,256]),
                            'prev_pair': np.zeros([L,L,128]),
                            'prev_pos': np.zeros([L,37,3])}
              
    if self._args["recycle_mode"] in ["backprop","add_prev"]:
      self.opt["recycles"] = self._runner.config.model.num_recycle

    recycles = self.opt["recycles"]

    if self._args["recycle_mode"] == "average":
      # average gradient across recycles
      grad = []
      for r in range(recycles+1):
        if backprop:
          (loss, aux), _grad = self._grad_fn(self.params, model_params, self._inputs, self.key(), self.opt)
          grad.append(_grad)
        else:
          loss, aux = self._fn(self.params, model_params, self._inputs, self.key(), self.opt)
        self._inputs["prev"] = aux["prev"]
      if backprop:
        grad = jax.tree_map(lambda *x: jnp.asarray(x).mean(0), *grad)

    else:
      if self._args["recycle_mode"] == "sample":
        self.opt["recycles"] = jax.random.randint(self.key(), [], 0, recycles+1)
      
      if backprop:
        (loss, aux), grad = self._grad_fn(self.params, model_params, self._inputs, self.key(), self.opt)
      else:
        loss, aux = self._fn(self.params, model_params, self._inputs, self.key(), self.opt)

    aux["recycles"] = self.opt["recycles"]
    self.opt["recycles"] = recycles
    
    if backprop:
      return (loss, aux), grad
    else:
      return loss, aux
    
  def save_results(self, save_best=False, verbose=1):
    '''save the results and update trajectory'''

    # save best result
    if save_best and self.loss < self._best_loss:
      self._best_loss = self.loss
      self._best_aux = self._best_outs = self.aux

    # save losses
    losses = jax.tree_map(float,self.aux["losses"])

    losses.update({"models":         self.aux["model_num"],
                   "recycles":   int(self.aux["recycles"]),
                   "hard":     float(self.opt["hard"]),
                   "soft":     float(self.opt["soft"]),
                   "temp":     float(self.opt["temp"]),
                   "loss":     float(self.loss)})
    
    if self.protocol in ["fixbb","partial"] or (self.protocol == "binder" and self._args["redesign"]):
      # compute sequence recovery
      _aatype = self.aux["seq"]["hard"].argmax(-1)
      if "pos" in self.opt:
        _aatype = _aatype[...,self.opt["pos"]]
      losses["seqid"] = float((_aatype == self._wt_aatype).mean())

    # print results
    if verbose and (self._k % verbose) == 0:
      keys = ["models","recycles","hard","soft","temp","seqid","loss",
              "msa_ent","plddt","pae","helix","con","i_pae","i_con",
              "sc_fape","sc_rmsd","dgram_cce","fape","rmsd"]
      print(dict_to_str(losses, filt=self.opt["weights"], print_str=f"{self._k}",
                        keys=keys, ok="rmsd"))

    # save trajectory
    traj = {"losses":losses, "seq":np.asarray(self.aux["seq"]["pseudo"])}
    traj["xyz"] = np.asarray(self.aux["final_atom_positions"][:,1,:])
    traj["plddt"] = np.asarray(self.aux["plddt"])
    if "pae" in self.aux: traj["pae"] = np.asarray(self.aux["pae"])
    for k,v in traj.items(): self._traj[k].append(v)

  # ---------------------------------------------------------------------------------
  # example design functions
  # ---------------------------------------------------------------------------------
  def design(self, iters=100,
             soft=None, e_soft=None,
             temp=None, e_temp=None,
             hard=None, e_hard=None,
             opt=None, weights=None, dropout=None,
             backprop=True, callback=None, save_best=False, verbose=1):
      
    # update options/settings (if defined)
    self.set_opt(opt, dropout=dropout)
    self.set_weights(weights)

    if soft is None: soft = self.opt["soft"]
    if temp is None: temp = self.opt["temp"]
    if hard is None: hard = self.opt["hard"]
    if e_soft is None: e_soft = soft
    if e_temp is None: e_temp = temp
    if e_hard is None: e_hard = hard
    
    for i in range(iters):
      self.set_opt(soft=(soft+(e_soft-soft)*((i+1)/iters)),
                   hard=(hard+(e_hard-hard)*((i+1)/iters)),
                   temp=(e_temp+(temp-e_temp)*(1-(i+1)/iters)**2))
      
      # decay learning rate based on temperature
      lr_scale = (1 - self.opt["soft"]) + (self.opt["soft"] * self.opt["temp"])
      
      self.step(lr_scale=lr_scale, backprop=backprop, callback=callback,
                save_best=save_best, verbose=verbose)

  def design_logits(self, iters=100, **kwargs):
    '''optimize logits'''
    self.design(iters, soft=0, hard=0, **kwargs)

  def design_soft(self, iters=100, **kwargs):
    ''' optimize softmax(logits/temp)'''
    self.design(iters, soft=1, hard=0, **kwargs)
  
  def design_hard(self, iters=100, **kwargs):
    ''' optimize argmax(logits)'''
    self.design(iters, soft=1, hard=1, **kwargs)

  # ---------------------------------------------------------------------------------
  # experimental
  # ---------------------------------------------------------------------------------

  def design_2stage(self, soft_iters=100, temp_iters=100, hard_iters=50,
                    models=1, dropout=True, **kwargs):
    '''two stage design (soft→hard)'''
    self.set_opt(models=models, sample_models=True) # sample models
    self.design(soft_iters, soft=1, temp=1, dropout=dropout, **kwargs)
    self.design(temp_iters, soft=1, temp=1, dropout=dropout,  e_temp=1e-2, **kwargs)
    self.set_opt(models=5) # use all models
    self.design(hard_iters, soft=1, temp=1e-2, dropout=False, hard=True, save_best=True, **kwargs)

  def design_3stage(self, soft_iters=300, temp_iters=100, hard_iters=10,
                    models=1, dropout=True, **kwargs):
    '''three stage design (logits→soft→hard)'''
    self.set_opt(models=models, sample_models=True) # sample models
    self.design(soft_iters, soft=0, temp=1,    hard=0, e_soft=1,    dropout=dropout, **kwargs)
    self.design(temp_iters, soft=1, temp=1,    hard=0, e_temp=1e-2, dropout=dropout, **kwargs)
    self.set_opt(models=5) # use all models
    self.design(hard_iters, soft=1, temp=1e-2, hard=1, dropout=False, save_best=True, **kwargs)

  def design_semigreedy(self, iters=100, tries=20, models=1,
                        use_plddt=True, save_best=True,
                        verbose=1):
    '''semigreedy search'''    
    self.set_opt(hard=True, dropout=False,
                 models=models, sample_models=False)
    if self._k == 0:
      self.run(backprop=False)

    def mut(seq, plddt=None):
      '''mutate random position'''
      L,A = seq.shape[-2:]
      while True:
        if plddt is None:
          i = jax.random.randint(self.key(),[],0,L)
        else:
          p = (1-plddt)/(1-plddt).sum(-1,keepdims=True)
          # bias mutations towards positions with low pLDDT
          # https://www.biorxiv.org/content/10.1101/2021.08.24.457549v1
          i = jax.random.choice(self.key(),jnp.arange(L),[],p=p)

        a = jax.random.randint(self.key(),[],0,A)
        if seq[0,i,a] == 0: break      
      return seq.at[:,i,:].set(jnp.eye(A)[a])

    def get_seq():
      return jax.nn.one_hot(self.params["seq"].argmax(-1),20)

    # optimize!
    for i in range(iters):
      seq = get_seq()
      plddt = None
      if use_plddt:
        plddt = np.asarray(self.aux["plddt"])
        if self.protocol == "binder":
          plddt = plddt[self._target_len:]
        else:
          plddt = plddt[:self._len]
      
      buff = []
      for _ in range(tries):
        self.params["seq"] = mut(seq, plddt)
        self.run(backprop=False)
        buff.append({"aux":self.aux, "loss":self.loss, "seq":self.params["seq"]})
      
      # accept best      
      buff = buff[np.argmin([x["loss"] for x in buff])]
      self.aux, self.loss, self.params["seq"] = buff["aux"], buff["loss"], buff["seq"]
      self._k += 1
      self.save_results(save_best=save_best, verbose=verbose)

  # ---------------------------------------------------------------------------------

  def template_predesign(self, iters=100, soft=True, hard=False, 
                         temp=1.0, dropout=True, **kwargs):
    '''use template for predesign stage, then increase template dropout until gone'''
    self.set_opt(hard=hard, soft=soft, dropout=dropout, temp=temp)
    for i in range(iters):
      self.opt["template"]["dropout"] = (i+1)/iters
      self.step(**kwargs)