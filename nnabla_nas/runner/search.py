import os
from collections import OrderedDict

import nnabla as nn
import nnabla.functions as F
import nnabla.utils.learning_rate_scheduler as LRS
from nnabla.logger import logger

from .. import utils as ut
from ..dataset import DataLoader
from ..dataset.cifar10 import cifar10
from ..optimizer import Optimizer
from ..visualization import visualize


class Searcher(object):
    """
    Searching the best architecture.
    """
    def __init__(self, model, conf):
        self.model = model
        self.arch_modules = model.get_arch_modues()
        self.conf = conf
        self.criteria = lambda o, t: F.mean(F.softmax_cross_entropy(o, t))
        self.evaluate = lambda o, t:  F.mean(F.top_n_error(o, t))
        self.w_micros = self.conf['batch_size'] // self.conf['mini_batch_size']

        # dataset configuration
        data = cifar10(conf['mini_batch_size'], True)
        train_transform, valid_transform = ut.dataset_transformer(conf)
        split = int(conf['train_portion'] * data.size)

        self.loader = {
            'model': DataLoader(
                data.slice(rng=None, slice_start=0, slice_end=split),
                train_transform
            ),
            'arch': DataLoader(
                data.slice(rng=None, slice_start=split, slice_end=data.size),
                valid_transform
            )
        }

        # solver configurations
        self.optimizer = dict()
        for key in ['model', 'arch']:
            optim = conf[key + '_optimizer'].copy()
            lr_scheduler = ut.get_object_from_dict(
                module=LRS.__dict__,
                args=optim.pop('lr_scheduler', None)
            )
            solver = optim['solver']
            self.optimizer[key] = Optimizer(
                retain_state=conf['mode'] == 'sample',
                weight_decay=optim.pop('weight_decay', None),
                grad_clip=optim.pop('grad_clip', None),
                lr_scheduler=lr_scheduler,
                name=solver.pop('name'), **solver
            )

        # placeholders
        self.placeholder = OrderedDict({
            'model': {
                'input':  nn.Variable(model.input_shape),
                'target': nn.Variable((conf['mini_batch_size'], 1))
            },
            'arch': {
                'input': nn.Variable(model.input_shape),
                'target': nn.Variable((conf['mini_batch_size'], 1))
            }
        })

    def run(self):
        """Run the training process."""
        conf = self.conf
        model = self.model
        optim = self.optimizer
        one_epoch = len(self.loader['model']) // conf['batch_size']

        out_path = conf['output_path']
        model_path = os.path.join(out_path, conf['model_name'])
        log_path = os.path.join(out_path, 'search_config.json')
        arch_file = model_path + '.json'

        # monitor the training process
        monitor = ut.ProgressMeter(one_epoch, path=out_path)
        logger.info('Experimental settings are saved to ' + log_path)
        ut.write_to_json_file(content=conf, file_path=log_path)

        # sample computational graphs
        self._sample(verbose=True)

        for cur_epoch in range(conf['epoch']):
            monitor.reset()

            for i in range(one_epoch):
                if conf['mode'] == 'sample':
                    self._sample()

                reward = 0
                for mode, ph in self.placeholder.items():
                    optim[mode].zero_grad()
                    training = (mode == 'model')
                    
                    for _ in range(self.w_micros):
                        ph['input'].d, ph['target'].d = self.loader[mode].next()
                        ph['loss'].forward(clear_no_need_grad=True)
                        ph['err'].forward(clear_buffer=True)
                        if training or conf['mode'] != 'sample':
                            ph['loss'].backward(clear_buffer=True)

                        error = ph['err'].d.copy()
                        loss = ph['loss'].d.copy() * self.w_micros

                        monitor.update(mode + '_loss', loss, conf['mini_batch_size'])
                        monitor.update(mode + '_err', error, conf['mini_batch_size'])

                        if not training:
                            reward += loss

                    optim[mode].update()

                if conf['mode'] == 'sample':
                    self._reinforce_update(reward)

                optim['arch'].update()

                if i % conf['print_frequency'] == 0:
                    monitor.display(i)

            # saving the architecture parameters
            if conf['shared_params']:
                ut.save_dart_arch(model, arch_file)
                for tag, img in visualize(arch_file, out_path).items():
                    monitor.write_image(tag, img, cur_epoch)
            else:
                model.save_parameters(model_path + '.h5',
                                      model.get_arch_parameters())
            monitor.write(cur_epoch)
            logger.info('Epoch %d: lr=%.5f\tErr=%.3f\tLoss=%.3f' %
                        (cur_epoch, optim['model'].get_learning_rate(),
                         monitor['arch_err'].avg, monitor['arch_loss'].avg))

        monitor.close()

        return self

    def _reinforce_update(self, reward):
        for m in self.arch_modules:
            m._update_alpha_grad()
        # perform control variate
        for v in self.optimizer['arch'].get_parameters().values():
            v.g *= reward - self.conf['arch_optimizer']['control_variate']

    def _sample(self, verbose=False):
        """Sample new graphs, one for model training and one for arch training."""
        if self.conf['mode'] == 'sample':
            for m in self.arch_modules:
                m._update_active_idx()

        for mode, ph in self.placeholder.items():
            training = (mode == 'model')
            self.model.train(training)

            # loss and error
            image = ut.image_augmentation(ph['input'])
            ph['output'] = self.model(image).apply(persistent=True)
            ph['loss'] = self.criteria(ph['output'], ph['target']) / self.w_micros
            ph['err'] = self.evaluate(
                ph['output'].get_unlinked_variable(), ph['target'])
            ph['loss'].apply(persistent=True)
            ph['err'].apply(persistent=True)

            # set parameters to the optimizer
            params = self.model.get_net_parameters(grad_only=True) if training\
                else self.model.get_arch_parameters(grad_only=True)
            self.optimizer[mode].set_parameters(params)
        
        if verbose:
            model_size = ut.get_params_size(self.optimizer['model'].get_parameters())/1e6
            arch_size = ut.get_params_size(self.optimizer['arch'].get_parameters())/1e6
            logger.info('Model size={:.6f} MB\t Arch size={:.6f} MB'.format(model_size, arch_size))

        return self
