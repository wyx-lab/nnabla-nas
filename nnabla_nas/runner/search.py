from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

import nnabla as nn
from nnabla.logger import logger

from .. import utils as ut
from ..utils import ProgressMeter


class Searcher(object):
    r"""Searching the best architecture.

    Args:
        model ([type]): [description]
        placeholder ([type]): [description]
        optimizer ([type]): [description]
        dataloader ([type]): [description]
        regularizer ([type]): [description]
        criteria ([type]): [description]
        evaluate ([type]): [description]
        args ([type]): [description]
    """

    def __init__(self, model,  placeholder, optimizer, dataloader, regularizer,
                 criteria, evaluate, args):
        self.model = model
        self.criteria = criteria
        self.evaluate = evaluate
        self.dataloader = dataloader
        self.regularizer = regularizer
        self.optimizer = optimizer
        self.placeholder = placeholder
        self.args = args

        # aditional argurments
        self.accum_train = self.args.bs_train // self.args.mbs_train
        self.accum_valid = self.args.bs_valid // self.args.mbs_valid
        self.one_epoch = len(self.dataloader['train']) // args.bs_train
        self.monitor = ProgressMeter(self.one_epoch, path=args.output_path)

        self.callbacks_at_beginning()

    def run(self):
        """Run the training process."""
        self._warmup()
        for cur_epoch in range(self.args.epoch):
            self.monitor.reset()
            lr = self.optimizer['train'].get_learning_rate()
            logger.info(f'Running epoch={cur_epoch}\tlr={lr:.5f}')
            for i in range(self.one_epoch):
                self.update_model_step()
                self.update_arch_step()
                if i % (self.args.print_frequency) == 0:
                    self.monitor.display(i)
            # saving the architecture parameters
            self.save_output()
            # logging output
            self.monitor.write(cur_epoch)
        self.monitor.close()
        return self

    def _warmup(self):
        """Performs warmup for the model on training."""
        for cur_epoch in range(self.args.warmup):
            self.monitor.reset()
            lr = self.optimizer['warmup'].get_learning_rate()
            logger.info(f'warm-up epoch={cur_epoch}\tlr={lr:.5f}')
            for i in range(self.one_epoch):
                self.update_model_step(key='warmup')
                if i % (self.args.print_frequency) == 0:
                    self.monitor.display(i)

    def _sample_train_net(self, key='train'):
        """Sample a network for model update."""
        self.sample_network()
        self.model.apply(training=True)
        p = self.placeholder['train']
        image = ut.image_augmentation(p['input'])
        p['output'] = self.model(image).apply(persistent=True)
        p['loss'] = (self.criteria(p['output'], p['target'])
                     / self.accum_train)
        p['err'] = self.evaluate(
            p['output'].get_unlinked_variable(),
            p['target']
        )
        p['loss'].apply(persistent=True)
        p['err'].apply(persistent=True)
        # setup parameters
        self.optimizer[key].set_parameters(
            self.model.get_net_parameters(grad_only=True)
        )

    def _sample_search_net(self):
        """Sample a network for arch update."""
        self.sample_network()
        self.model.apply(training=False)
        ph = self.placeholder['valid']
        ph['output'] = self.model(ph['input']).apply(persistent=True)
        ph['loss'] = self.criteria(ph['output'], ph['target'])/self.accum_valid
        ph['err'] = self.evaluate(
            ph['output'].get_unlinked_variable(),
            ph['target']
        )
        ph['loss'].apply(persistent=True)
        ph['err'].apply(persistent=True)
        self.optimizer['valid'].set_parameters(
            self.model.get_arch_parameters(grad_only=True)
        )

    def sample_network(self):
        pass

    def callbacks_at_beginning(self):
        pass

    def save_output(self):
        nn.save_parameters(
            os.path.join(self.args.output_path, 'arch.h5'),
            self.model.get_parameters()
        )

    def update_model_step(self):
        raise NotImplementedError

    def update_arch_step(self):
        raise NotImplementedError


class DartsSeacher(Searcher):

    def callbacks_at_beginning(self):
        self._sample_train_net()
        self._sample_search_net()

    def update_model_step(self, key='train'):
        bz, p = self.args.mbs_train, self.placeholder['train']
        self.optimizer[key].zero_grad()
        for _ in range(self.accum_train):
            p['input'].d, p['target'].d = self.dataloader['train'].next()
            p['loss'].forward(clear_no_need_grad=True)
            p['loss'].backward(clear_buffer=True)
            p['err'].forward(clear_buffer=True)
            loss, err = p['loss'].d.copy(),  p['err'].d.copy()
            self.monitor.update('train_loss', loss * self.accum_train, bz)
            self.monitor.update('train_err', err, bz)
        self.optimizer[key].update()

    def update_arch_step(self):
        bz, p = self.args.mbs_valid, self.placeholder['valid']
        self._sample_search_net()
        self.optimizer['valid'].zero_grad()
        for i in range(self.accum_valid):
            p['input'].d, p['target'].d = self.dataloader['valid'].next()
            p['loss'].forward(clear_buffer=True)
            p['err'].forward(clear_buffer=True)
            loss, err = p['loss'].d.copy(),  p['err'].d.copy()
            self.monitor.update('valid_loss', loss * self.accum_valid, bz)
            self.monitor.update('valid_err', err, bz)
        self.optimizer['valid'].update()


class ProxylessNasSearcher(Searcher):

    def callbacks_at_beginning(self):
        self.arch_modules = self.model.get_arch_modules()
        self._reward = 0

    def sample_network(self):
        for m in self.arch_modules:
            m.update_active_index()

    def update_model_step(self, key='train'):
        """Update the model parameters."""
        bz, p = self.args.mbs_train, self.placeholder['train']
        self._sample_train_net()
        self.optimizer[key].zero_grad()
        for _ in range(self.accum_train):
            p['input'].d, p['target'].d = self.dataloader['train'].next()
            p['loss'].forward(clear_no_need_grad=True)
            p['loss'].backward(clear_buffer=True)
            p['err'].forward(clear_buffer=True)
            loss, err = p['loss'].d.copy(),  p['err'].d.copy()
            self.monitor.update('train_loss', loss * self.accum_train, bz)
            self.monitor.update('train_err', err, bz)
        self.optimizer[key].update()

    def update_arch_step(self):
        """Update the arch parameters."""
        beta, n_iter = 0.9, 5
        bz, p = self.args.mbs_valid, self.placeholder['valid']
        data = [self.dataloader['valid'].next()
                for i in range(self.accum_valid)]
        rewards, grads = [], []
        for _ in range(n_iter):
            reward = 0
            self._sample_search_net()
            for i in range(self.accum_valid):
                p['input'].d, p['target'].d = data[i]
                p['loss'].forward(clear_buffer=True)
                p['err'].forward(clear_buffer=True)
                loss, err = p['loss'].d.copy(),  p['err'].d.copy()
                reward += (1 - err) / self.accum_valid
                self.monitor.update('valid_loss', loss * self.accum_valid, bz)
                self.monitor.update('valid_err', err, bz)
            # adding contraints
            for k, v in self.regularizer.items():
                value = v['reg'].get_estimation(self.model)
                reward *= (v['bound'] / value)**v['weight']
                self.monitor.update(k, value, 1)
            rewards.append(reward)
            grads.append([m._alpha.g.copy() for m in self.arch_modules])
            self.monitor.update('reward', reward, self.args.bs_valid)
        # compute gradients
        for j, m in enumerate(self.arch_modules):
            m._alpha.grad.zero()
            for i, r in enumerate(rewards):
                m._alpha.g += (r - self._reward) * grads[i][j] / n_iter
        self.optimizer['valid'].update()
        self._reward = beta*sum(rewards)/n_iter + (1 - beta)*self._reward
