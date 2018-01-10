from copy import deepcopy
import abc
import hashlib
try:
    from itertools import izip_longest as zip_longest
except:
    from itertools import zip_longest
import json
import logging
import os
import sys
from time import time

import dataset_loaders
import numpy as np
import tensorflow as tf
from tensorflow.python.training.training import CheckpointSaverHook
from tensorflow.python.training.monitored_session import (MonitoredSession,
                                                          ChiefSessionCreator)
from tensorflow.python.training.summary_io import SummaryWriterCache
from tqdm import tqdm

import gflags
from hooks import EarlyStopHook
from optimization import (apply_lr_decay, average_gradients,
                          compute_and_process_grads, get_optimizer)
from utils import (recursive_dict_stack, recursive_truncate_dict,
                   save_repos_hash, split_in_chunks, squash_maybe, TqdmHandler,
                   uniquify_path)

# config module load all flags from source files
import config  # noqa

FLAGS = gflags.FLAGS
gflags.DEFINE_bool('help', False, 'If True, shows this message')
gflags.DEFINE_bool('debug', False, 'If True, enable tensorflow debug')


class Experiment(object):

    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def build_loss(self, placeholders, model_outs, is_training):
        pass

    @abc.abstractmethod
    def build_model(self, placeholders, is_training):
        pass

    # def validate_fn(self, input_placeholders, graph_outs, which_set):
    #     return dict

    def __init__(self, flags_argv, Optimizer=None):
        """Create an Experiment object

        Parameters
        ----------
        flags_argv: list
            A list of flags argument for gflags
        Optimizer: :class:`DistributedOptimizer`
            Optional. An optimizer object to be used in the optimization
            phase.
        """
        gflags.mark_flags_as_required(['dataset'])
        self.UserOptimizer = Optimizer

        # ============ Parse gflags
        try:
            FLAGS(flags_argv)  # parse flags
        except gflags.FlagsError as e:
            print('Usage: %s ARGS\n%s\n\nError: %s' % (flags_argv[0], FLAGS,
                                                       e))
            sys.exit(0)

        # Show help message
        if FLAGS.help:
            print('%s' % FLAGS)
            sys.exit(0)

        self.process_cfg_flags()

        # Init variables
        self._graph_built = False
        self.cum_grads_and_vars = {}
        self.val_graph_outs = {}
        self.avg_loss = {True: {}, False: {}}

        # Build the graph
        self.__build_graph()

    def process_cfg_flags(self):
        # Convert FLAGS to namespace, so we can modify it
        from argparse import Namespace
        cfg = Namespace()
        fl = FLAGS.FlagDict()
        cfg.__dict__ = {k: el.value for (k, el) in fl.iteritems()}
        gflags.cfg = cfg

        # ============ Hash, (gsheet) and checkpoints
        # Exclude non JSONable and not interesting objects
        exclude_list = ['checkpoints_basedir', 'checkpoints_to_keep',
                        'checkpoints_save_secs', 'checkpoints_save_steps',
                        'data_queues_size', 'dataset', 'debug', 'devices',
                        'group_summaries', 'help', 'hyperparams_summaries',
                        'max_epochs', 'min_epochs', 'model_name',
                        'model_suffix', 'nthreads', 'patience',
                        'restore_model', 'restore_suite', 'suite_name',
                        'thresh_loss', 'train_summary_freq', 'use_threads',
                        'val_every_epochs', 'val_on_sets', 'val_skip_first',
                        'validate']
        if hasattr(self, 'extra_exclude_list'):
            exclude_list.extend(self.extra_exclude_list)
        cfg_dump_dict = {k: deepcopy(v) for (k, v) in cfg.__dict__.iteritems()
                         if k not in exclude_list}
        h = hashlib.md5()
        h.update(str(cfg_dump_dict))
        cfg.hash = h.hexdigest()
        save_repos_hash(cfg_dump_dict, cfg.model_name, ['tensorflow',
                                                        'dataset_loaders',
                                                        'main_loop_tf'])
        self._cfg_dump_dict = cfg_dump_dict

        checkpoints_path = cfg.checkpoints_basedir
        if cfg.suite_name != '':
            checkpoints_path = os.path.join(checkpoints_path, cfg.suite_name)
        cfg.checkpoints_path = checkpoints_path

        model_name = cfg.model_name if cfg.model_name != '' else cfg.hash
        if cfg.model_suffix != '':
            model_name += '_' + cfg.model_suffix
        save_path = os.path.join(checkpoints_path, model_name)
        cfg.model_name = model_name

        # Save path
        if cfg.restore_model.lower() not in ['', 'true']:  # false or custom
            # If the model should not be restored from a checkpoint,
            # and the save path exists, make the save path unique by
            # adding an incremental suffix
            _, save_path = uniquify_path(save_path)
        # Restore path
        if cfg.restore_model.lower() not in ['', 'true', 'false']:
            # A specific restore path has been provided
            restore_path = cfg.checkpoints_basedir
            if cfg.restore_suite != '':
                restore_path = os.path.join(restore_path,
                                            cfg.restore_suite)
            restore_path = os.path.join(restore_path, cfg.restore_model)
        elif cfg.restore_model.lower() == 'false':
            # Disable restore
            restore_path = None
        else:
            # Restore path == save path
            restore_path = os.path.join(save_path)
        cfg.save_path = save_path
        cfg.restore_path = restore_path
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        cfg_dump_path = os.path.join(save_path, 'params_and_hashes')
        cfg_load_path, cfg_dump_path = uniquify_path(cfg_dump_path, 'txt')
        self.cfg_load_path, self.cfg_dump_path = cfg_load_path, cfg_dump_path

        # ============ A bunch of derived params
        cfg._FLOATX = 'float32'
        # Infer devices from CUDA_VISIBLE_DEVICES if not specified
        if cfg.devices is None:
            cfg.devices = ['/gpu:%s' % d for d in
                           os.environ['CUDA_VISIBLE_DEVICES'].split(',')]
        cfg.num_gpus = len([el for el in cfg.devices if 'gpu' in el])
        cfg.num_cpus = len([el for el in cfg.devices if 'cpu' in el])
        cfg.num_devs = cfg.num_gpus + cfg.num_cpus

        # ============ Dataset init
        try:
            Dataset = getattr(dataset_loaders, cfg.dataset + 'Dataset')
        except AttributeError:
            Dataset = getattr(dataset_loaders, cfg.dataset.capitalize() +
                              'Dataset')

        self.Dataset = Dataset
        # Add dataset extra parameters specific for the dataset
        dataset_params = cfg.train_extra_params
        dataset_params['batch_size'] = cfg.batch_size * cfg.num_devs
        data_augm_kwargs = dataset_params['data_augm_kwargs'] = {}
        if cfg.crop_mode == 'smart':
            data_augm_kwargs['crop_mode'] = cfg.crop_mode
            data_augm_kwargs['smart_crop_threshold'] = cfg.smart_crop_threshold
            search_step = cfg.smart_crop_search_step
            data_augm_kwargs['smart_crop_search_step'] = search_step
        dataset_params['data_augm_kwargs']['crop_size'] = cfg.crop_size
        dataset_params['data_augm_kwargs']['return_optical_flow'] = cfg.of
        dataset_params['return_one_hot'] = False
        dataset_params['return_01c'] = True
        if cfg.seq_per_subset:
            dataset_params['seq_per_subset'] = cfg.seq_per_subset
        if cfg.overlap is not None:
            dataset_params['overlap'] = cfg.overlap
        if cfg.seq_length:
            dataset_params['seq_length'] = cfg.seq_length

            ret_ext_seq = cfg.return_extended_sequences
            ret_middle_frame = cfg.return_middle_frame_only
            dataset_params['return_extended_sequences'] = ret_ext_seq
            dataset_params['return_middle_frame_only'] = ret_middle_frame

        dataset_params['use_threads'] = cfg.use_threads
        dataset_params['nthreads'] = cfg.nthreads
        dataset_params['queues_size'] = cfg.data_queues_size
        dataset_params['remove_per_img_mean'] = cfg.remove_per_img_mean
        dataset_params['divide_by_per_img_std'] = cfg.divide_by_per_img_std
        dataset_params['remove_mean'] = cfg.remove_mean
        dataset_params['divide_by_std'] = cfg.divide_by_std
        cfg.dataset_params = dataset_params
        cfg.valid_params = deepcopy(cfg.dataset_params)
        cfg.valid_params.update({
            'batch_size': cfg.val_batch_size * cfg.num_devs,
            'seq_per_subset': 0,
            'overlap': cfg.val_overlap,
            'shuffle_at_each_epoch': (cfg.val_overlap is not None and
                                      cfg.val_overlap != 0),
            'one_subset_per_batch': True,  # prevent multiple subsets
            'use_threads': False,  # prevent shuffling
            # prevent crop
            'data_augm_kwargs': {'return_optical_flow': cfg.of}})
        # Add dataset extra parameters specific for each dataset
        cfg.valid_params.update(cfg.val_extra_params)

        # Create temporary dataset object (training/validation) to get
        # dynamic class elements (e.g. data_shape)
        train_temp = Dataset(
            which_set='train',
            return_list=False,
            **cfg.dataset_params)
        valid_temp = Dataset(
            which_set='valid',
            **cfg.valid_params)

        # TODO: check fvisin comment, this is not the correct behavior, but
        # it's done in order to work with movingMNST iirc
        if cfg.seq_length:
            cfg.input_shape = [None, cfg.seq_length] + list(
                train_temp.next()['data'].shape[2:])
            cfg.val_input_shape = [None, cfg.seq_length] + list(
                valid_temp.next()['data'].shape[2:])

            if cfg.of:
                cfg.input_shape[-1] = 6
                cfg.val_input_shape[-1] = 6

            if cfg.crop_size:
                cfg.input_shape[2:4] = cfg.crop_size
        else:
            cfg.input_shape = [None] + list(
                train_temp.next()['data'].shape[1:])
            cfg.val_input_shape = [None] + list(
                valid_temp.next()['data'].shape[1:])
            if cfg.crop_size:
                cfg.input_shape[1:3] = cfg.crop_size

        cfg.void_labels = getattr(Dataset, 'void_labels', [])
        cfg.nclasses = Dataset.non_void_nclasses
        cfg.nclasses_w_void = Dataset.nclasses
        tf.logging.info('{} classes ({} non-void):'.format(cfg.nclasses_w_void,
                                                           cfg.nclasses))
        # Destroy temporary dataset objects
        train_temp.finish()
        valid_temp.finish()
        del(train_temp, valid_temp)

        self.cfg = cfg

    def get_placeholders(self):
        """Create the graph's placeholders

        Return two lists of placeholders, for training and validation
        respectively. Keeping them separated allows to train on cropped
        inputs and validate at full size easily.

        Each list will contain a dictionary per device, with all the
        placeholders that will be used by the graph on that device.
        """
        cfg = self.cfg

        train_placeholders = []
        val_placeholders = []
        # Iterate over the devices
        for i, _ in enumerate(range(cfg.num_devs)):
            train_ins = tf.placeholder(dtype=cfg._FLOATX,
                                       shape=cfg.input_shape,
                                       name='train_inputs_per_gpu_%i' % i)
            val_ins = tf.placeholder(dtype=cfg._FLOATX,
                                     shape=cfg.val_input_shape,
                                     name='val_inputs_per_gpu_%i' % i)
            targets = tf.placeholder(dtype=np.int32,
                                     shape=[None],  # flattened
                                     name='targets_per_gpu_%i' % i)
            # Note, the keys have to match those of the minibatch
            train_placeholders.append({'data': train_ins,
                                       'labels': targets})
            val_placeholders.append({'data': val_ins,
                                     'labels': targets})
        return train_placeholders, val_placeholders

    def __build_graph(self):
        if self._graph_built:
            raise RuntimeError('You cannot build the graph twice.')
        self._graph_built = True
        cfg = self.cfg

        # ============ Train/validation
        # Load data
        # init_epoch = 0
        # prev_history = None
        # best_loss = np.Inf
        # best_val = np.Inf if early_stop_strategy == 'min' else -np.Inf
        # val_metrics_ext = ['val_' + m for m in val_metrics]
        # history_path = tmp_path + save_name + '.npy'
        # if cfg.reload_weights:
        #     # Reload weights
        #     pass

        tf.logging.info("Building the model ...")
        self.graph = tf.get_default_graph()
        with self.graph.as_default():
            # Plant the seeds, let them grow...
            tf.set_random_seed(cfg.random_seed)
            np.random.seed(cfg.random_seed)

            self.global_step = tf.get_variable(
                'global_step', [],
                initializer=tf.constant_initializer(0),
                trainable=False, dtype='int32')
            self.sym_num_devs = tf.placeholder(np.int32, shape=None,
                                               name='num_devs')
            self.sym_num_batches = tf.placeholder(np.int32, shape=None,
                                                  name='num_batches')
            self.sym_prev_err = tf.placeholder(shape=(), dtype=cfg._FLOATX,
                                               name='prev_err')

            # Create a list of input placeholders for each device.
            # When the batchsize is not big enough to fill all of them we
            # would want to use a subset of the placeholders, but TF raises
            # a 'negative shape error' if a placeholder is not fed. Instead,
            # we provide all of them with values but we use n_spits to
            # select which of the inputs to process (and perform gradient
            # descent on) and which to ignore.
            # At runtime, we replicate the input data to feed all the
            # placeholders (even if it's internally ignored). We could use
            # placeholder_with_default to assign a value to it's input but
            # the batch_size might change dynamically, so we rather
            # replicate the input at runtime.
            train_placeholders, val_placeholders = self.get_placeholders()
            self.per_dev_placeholders = {True: train_placeholders,
                                         False: val_placeholders}

            # Optimizer
            lr = apply_lr_decay(self.cfg, self.global_step)
            Optimizer = (self.UserOptimizer if self.UserOptimizer else
                         get_optimizer(cfg.optimizer))
            self.optimizer = Optimizer(learning_rate=lr,
                                       **cfg.optimizer_params)

            # Model compilation
            # -----------------
            # Model parameters on the FIRST device specified in cfg.devices
            # Gradient Average and the rest of the operations are on CPU
            with tf.device('/cpu:0'):
                # Build the training graph
                self.train_graph_outs = self.__build_device_graph(
                    which_set='train', is_training=True)

                # Build the validation graphs (reusing variables) for
                # each subset we want to run validation on. This is
                # necessary to build two ops, one to use all the devices
                # and a second to potentially use less if we cannot feed
                # all of them with the last batch.
                for s in cfg.val_on_sets:
                    self.val_graph_outs[s] = self.__build_device_graph(
                        which_set=s, is_training=False)

                # Create the hyperparameters summaries operations
                if cfg.hyperparams_summaries is not None:
                    with tf.name_scope('hyperparams_summaries'):
                        summary_text = []
                        for (k, vals) in cfg.hyperparams_summaries.iteritems():

                            header_list = []
                            text_list = []
                            for v in vals:
                                header_list.append('**' + v + '**')
                                text_list.append(str(getattr(cfg, v)))
                            header_tensor = tf.constant(header_list)
                            text_tensor = tf.constant(text_list)

                            summary = tf.summary.text(k, tf.reshape(tf.concat(
                                [header_tensor, text_tensor], axis=0),
                                [2, -1]))
                            summary_text.append(summary)
                        self.summary_text_op = tf.summary.merge(summary_text)

    def get_loss_extra_params(self):
        """Add extra parameters to the loss function

        Allow to potentially add extra parameters to the symbolic loss
        function"""
        return {}

    def get_grad_descent_var_list(self):
        """Select which variables to train

        Allow to potentially specify which symbolic variables to train on"""
        return None

    def dev_model_out_post(self, model_out, dev_placeholders, dev_stats_scope,
                           phase_set_dev, these_s):
        """Process the model output for visualization

        Allow to potentially symbolically postprocess the output of the
        model, e.g., for visualization, once the loss has been defined"""
        return model_out

    def dev_extra_summaries(self, stacked_model_outs, stacked_loss_outs,
                            is_training, dev_stats_scope, phase_set_dev,
                            these_s):
        """Add user-defined per-device summaries"""
        pass

    def extra_summaries(self, stacked_model_outs, stacked_loss_outs,
                        is_training, stats_scope, these_s):
        """Add user-defined global summaries"""
        pass

    def extra_graph_out(self, graph_out, stacked_model_outs, stacked_loss_outs,
                        is_training, merge_scope):
        """Add user-defined metrics to the graph

        Allow the user to define some extra metric into the graph. This
        should be returned via the graph_out dictionary and/or
        modifications to self"""
        # Concat and truncate the per_dev_placeholders to recover the
        # tensors that correspond to the part of the placeholders that
        # are actually used at run-time. When all the devices are in use
        # this corresponds to concatenating each per_dev_placeholder
        # into an aggregated placeholder. These are the placeholders
        # that the user should use to e.g., compute performance metrics
        # against the labels.
        with tf.name_scope(merge_scope):
            stacked_placeholders = {}
            for p in self.per_dev_placeholders[is_training]:
                recursive_dict_stack(p, stacked_placeholders)
            self.placeholders = recursive_truncate_dict(stacked_placeholders,
                                                        self.sym_num_devs)
        return graph_out

    def __build_device_graph(self, which_set, is_training):
        ''' Build the multiGPU graph of computation

        This function creates a copy of the computation graph on each GPU. The
        result of the computation of each GPU is stored in a "tower"
        Note that thanks to the use of name_scopes and variable_scopes,
        calling this function multiple times does not create multiple
        copies of the *Ops* and of the *Variables* (respectively), but
        rather only adds the Ops that change from one call to the other
        and reuses the same Variables.

        Furthermore, we accommodate for the case where some minibatches
        are smaller than the usual size and are not enough to feed all
        the devices. Since we cannot change the graph at runtime, we
        accomplish this by feeding the unused devices and discarding
        their output. To prevent the statistics of these unnecessary
        computation to be retrieved and visualized, we create several
        summary ops, to collect the summaries of the first device, of
        the first two devices, of the first three, .., and so on. This
        allows to choose at runtime which summary operations to call,
        depending on the batch size.  batch size.
        '''
        cfg = self.cfg
        reuse_variables = not is_training

        per_dev_placeholders = self.per_dev_placeholders[is_training]
        phase_set = 'T.' if is_training else 'V_' + which_set + '.'

        # Create "towers" with the model outputs/loss keys and a value
        # for each device
        stacked_model_outs = {}
        stacked_loss_outs = {}
        summaries = []
        for device in cfg.devices:
            device_str = device.replace('/', '').replace(':', '').lower()
            summaries.append(phase_set + device_str)
        these_s = summaries

        # Build a graph for each device, each with its input and output
        # placeholders. Collect the outputs in "towers"
        # -------------------------------------------------------------
        for dev_id, (dev, dev_placeholders) in enumerate(
                zip(cfg.devices, per_dev_placeholders)):
            device_str = 'dev' + str(dev_id)
            phase_set_dev = phase_set + device_str
            # NOTE The name scopes help organize the graph in tensorboard
            # The variable scopes are needed to reuse the variables among
            # the various graphs
            with tf.name_scope(phase_set_dev) as phase_set_dev_scope, \
                    tf.device(dev):
                with tf.variable_scope('model',
                                       reuse=reuse_variables) as model_scope:
                    # Model preactivation, activation (softmax) and prediction
                    # NOTE Will be then stacked in stacked_model_outs
                    model_out = self.build_model(dev_placeholders, is_training)
                    assert isinstance(model_out, dict), """
                        Your model should return a dictionary"""
                    assert 'out_preact' in model_out, """Your model
                        function should return a dictionary with attribute
                        'out_preact'!"""
                    assert 'out_act' in model_out, """Your model function
                        should return a dictionary with attribute 'out_act'!"""
                    assert 'pred' in model_out, """Your model function should
                        return a dictionary with at least attribute 'pred'!"""

                with tf.variable_scope('loss', reuse=reuse_variables):
                    reuse_variables = True  # Reuse from now on
                    loss_params = self.get_loss_extra_params()
                    loss_out = self.build_loss(dev_placeholders, model_out,
                                               is_training=is_training,
                                               **loss_params)
                assert loss_out is not None and isinstance(loss_out, dict), (
                    """Your loss should return a dictionary""")
                assert 'loss' in loss_out, """Your loss function should
                    return a dictionary with attribute 'loss'!"""
                assert 'components' in loss_out, """Your loss function should
                    return a dictionary with attribute 'components'
                    containing the list of terms that composes the total
                    loss!"""
                # Append this device's loss outs to those of prev devices
                recursive_dict_stack(loss_out, stacked_loss_outs)

                # Add '_stats' suffix to the name scope
                scope_str = phase_set_dev + '_aggregated_stats'
                with tf.name_scope(None):
                    with tf.name_scope(scope_str) as dev_stats_scope:
                        tf.summary.scalar('loss', loss_out['loss'], these_s)

                # Allow to potentially postprocess the output of the
                # model, e.g., for visualization, once the loss has been
                # computed
                with tf.variable_scope(model_scope):
                    model_out = self.dev_model_out_post(model_out,
                                                        dev_placeholders,
                                                        dev_stats_scope,
                                                        phase_set_dev + '.',
                                                        these_s)
                # Append this device's model outs to those of prev devices
                recursive_dict_stack(model_out, stacked_model_outs)

                if is_training:
                    # Compute gradients, add noise to the gradient and
                    # create the op to apply it if needed.
                    grads_and_vars = self.compute_and_process_grads(
                        loss_out=loss_out,
                        var_list=self.get_grad_descent_var_list(),
                        gate_gradients=None,
                        aggregation_method=None,
                        colocate_gradients_with_ops=True,
                        name=None,
                        grad_loss=None,
                        phase_set_dev=phase_set_dev + '.',
                        summaries=these_s)

                    # Gradient descent
                    # ----------------
                    # Expand the list of grads (one per device) of each var
                    for g, v in grads_and_vars:
                        # Append the grads of the current device
                        self.cum_grads_and_vars.setdefault(v, []).append(g)

            with tf.name_scope(phase_set_dev_scope):
                self.dev_extra_summaries(stacked_model_outs, stacked_loss_outs,
                                         is_training, dev_stats_scope,
                                         phase_set_dev + '.', these_s)

            # Print regularization
            for v in tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES):
                tf.logging.debug('Regularization losses:\n{}'.format(v))

            # Update the summaries that will be affected by the next graph.
            # We want summaries[0] to contain summaries relative to the
            # first device only, summaries[1] to the first and
            # second device and so on. This will be used in the main
            # loop to suppress some of the summaries when some of the
            # devices are not being used.
            #
            # Summary device0 device1 device2 ...
            #   0        X
            #   1        X       X
            #   1        X       X       X
            #  ...
            these_s = these_s[1:]

        # Average the gradients on CPU and do SGD
        if is_training:
            grad_ops = []
            update_ops = []
            for dev_id, dev in enumerate(cfg.devices):
                # Recover device name_space
                phase_set_dev = 'T.dev' + str(dev_id)
                update_ops += tf.get_collection(tf.GraphKeys.UPDATE_OPS,
                                                scope=phase_set_dev)

                scope = 'T.grads/uptodev' + str(dev_id)
                # Average the gradients over the devices processed so far
                avg_grads_and_vars = average_gradients(self.cum_grads_and_vars,
                                                       scope + '.',
                                                       up_to_dev=dev_id)

                # Impose graph dependency so that update operations are
                # computed even if they're are not explicit in the outputs os
                # session.run
                with tf.control_dependencies(update_ops):
                    grad_op = self.optimizer.apply_gradients(
                        avg_grads_and_vars, global_step=self.global_step,
                        name=scope)  # TODO ha senso? Probabilmente no

                # Create a *list* of gradient update ops. The t-th element of
                # the list updates the gradients of the devices *up to* the
                # t-th device
                grad_ops.append(grad_op)

            # Add the histograms of the gradients (all of them)
            for grad, var in avg_grads_and_vars:
                if grad is not None:
                    tf.summary.histogram(var.op.name + '.grads',
                                         grad, summaries)

        # Merge the towers on CPU
        # -----------------------
        # Convert the lists of tensors to concatenated tensors and keep
        # the first `num_devs`, i.e., dynamically select at runtime
        # which devices' outputs to consider
        with tf.name_scope(phase_set + 'merge_devs') as merge_scope:
            ps = phase_set
            curr_model_out = recursive_truncate_dict(stacked_model_outs,
                                                     self.sym_num_batches,
                                                     parent_k=ps + '/outs',
                                                     exact_len=cfg.num_devs)
            curr_loss_out = recursive_truncate_dict(stacked_loss_outs,
                                                    self.sym_num_devs,
                                                    parent_k=ps + '/losses',
                                                    exact_len=cfg.num_devs)

        # Plot the cumulative batch size of the aggregated predictions
        # for debugging purposes
        self.sym_batch_size = tf.shape(curr_model_out['pred'])[0]
        tf.summary.scalar(phase_set + 'control_flow/batch_size',
                          self.sym_batch_size, summaries)

        # We are trying to be flexible with the placeholders, so we
        # cannot use it at the moment. I'll keep it here for reference
        # on how to concatenate the placeholders of the device being used
        # # Concatenate the per-gpu placeholders to get a placeholder for the
        # # full list of gpus and one for the subset to be used for
        # # the minibatch with less batches
        # labels = tf.concat(self.labels_per_gpu, axis=0, name='concat_labels')
        # # Remove the unused batches from the flattened labels
        # # (equivalent to labels[:np.prod(merged_model_outs.shape)])
        # labels = labels[:tf.shape(
        #     tf.reshape(merged_model_outs['pred'], [-1]))[0]]

        # Compute the mean loss over the first num_devs devices. This
        # will also be used to update the loss summaries
        with tf.name_scope(phase_set + 'aggregated_stats') as stats_scope:
            avg_loss = tf.reduce_mean(curr_loss_out['loss'],
                                      name='avg_loss')
            self.avg_loss[is_training][which_set] = avg_loss

        #############
        # SUMMARIES #
        #############
        # Visualize the avg loss
        # The number of devices will be dynamically selected by the
        # numerical value assigned to num_devs at run-time) and used
        # to update the loss summaries correctly
        with tf.name_scope(stats_scope):
            tf.summary.scalar('avg_loss', avg_loss, summaries)

        if is_training:
            # Write the summary of the mean per-component loss over the first
            # num_devs devices (which will be dynamically selected at
            # run-time). We do not want to clutter the summaries with these
            # information for validation, but keep in mind that this could be
            # computed for validation as well
            with tf.name_scope(stats_scope):
                for (key, loss) in curr_loss_out['components'].iteritems():
                    avg_comp_loss = tf.reduce_mean(loss)
                    tf.summary.scalar('avg_loss_comp_%s' % key, avg_comp_loss,
                                      summaries)

                # Add the histograms for trainable variables
                for var in tf.trainable_variables():
                    var_name = var.op.name.replace('model/', '')
                    scope_str, var_name = squash_maybe('Train_var_act',
                                                       var_name)
                    tf.summary.histogram(scope_str + '_' + var_name, var,
                                         summaries)

        self.extra_summaries(curr_model_out, curr_loss_out,
                             is_training, stats_scope, these_s)

        # Create a list of summary ops that update the summary collections that
        # we used at graph creation time. Thanks to the way we decremented the
        # elements in the collections each time the graph for one device
        # was created, the n-th op in this list will update all the summaries
        # *up to* the n-th device. This will be used at run-time to ignore the
        # devices that are not in use when there are not enough batches to feed
        # all of them
        summary_ops = []
        for s in summaries:
            summary_ops.append(tf.summary.merge(tf.get_collection_ref(key=s)))

        graph_out = {
            'model_outs': curr_model_out,
            'summary_ops': summary_ops,
            }
        if is_training:
            graph_out['grad_ops'] = grad_ops

        # Allow the user to define custom metrics to be applied and
        # added to graph_out
        graph_out = self.extra_graph_out(graph_out, curr_model_out,
                                         curr_loss_out, is_training,
                                         merge_scope)

        return graph_out

    def run(self):
        with self._init_sess() as self.sess:
            if self.cfg.debug:
                from tensorflow.python import debug as tf_debug
                self.sess = tf_debug.LocalCLIDebugWrapperSession(self.sess)
                self.sess.add_tensor_filter("has_inf_or_nan",
                                            tf_debug.has_inf_or_nan)

            uninit_vars = self.sess.run(self._uninit_vars)
            if len(uninit_vars) > 0:
                raise RuntimeError('Uninitialized variables: {}'.format(
                    uninit_vars))

            # Start training loop
            return self._main_loop()

    def validate(self):
        with self._init_sess() as self.sess:
            if self.cfg.debug:
                from tensorflow.python import debug as tf_debug
                self.sess = tf_debug.LocalCLIDebugWrapperSession(self.sess)
                self.sess.add_tensor_filter("has_inf_or_nan",
                                            tf_debug.has_inf_or_nan)
            uninit_vars = self.sess.run(self._uninit_vars)
            if len(uninit_vars) > 0:
                raise RuntimeError('Uninitialized variables: {}'.format(
                    uninit_vars))

            validate_fn = getattr(self, "validate_fn", None)
            if validate_fn is not None:
                metrics_val = {}
                for s in self.cfg.val_on_sets:
                    metrics_val[s] = validate_fn(
                        self.val_graph_outs[s],
                        which_set=s)
                return metrics_val
            else:
                raise ValueError('No validation function defined! You '
                                 'should implement validate_fn')

    def get_hooks(self):
        # For more hooks see
        # https://www.tensorflow.org/api_guides/python/train#Training_Hooks
        cfg = self.cfg

        # Checkpoint saver hook
        save_secs = self.cfg.checkpoints_save_secs or None
        save_steps = self.cfg.checkpoints_save_steps or None
        saver = tf.train.Saver(
            name='Saver',
            save_relative_paths=True,
            max_to_keep=cfg.checkpoints_to_keep)
        saver_hook = CheckpointSaverHook(self.cfg.save_path,
                                         saver=saver,
                                         save_secs=save_secs,
                                         save_steps=save_steps,
                                         checkpoint_basename='model.ckpt')

        # Max epochs and early stopping
        early_stop_hook = EarlyStopHook(self)
        return [saver_hook, early_stop_hook]

    def _init_sess(self):
        with self.graph.as_default():
            # Initialize variables
            init_op = tf.global_variables_initializer()
            tf.add_to_collection(tf.GraphKeys.INIT_OP, init_op)
            local_init_op = tf.local_variables_initializer()
            tf.add_to_collection(tf.GraphKeys.LOCAL_INIT_OP, local_init_op)
            self._uninit_vars = tf.report_uninitialized_variables()

            # Retrieve summary writer and create MonitoredSession
            # https://github.com/tensorflow/tensorflow/issues/11350
            # https://github.com/tensorflow/tensorflow/blob/
            #   a7e225350abeed719f634ef71cd9d908424877b2/tensorflow/python/
            #   training/basic_session_run_hooks.py#L337
            self.summary_writer = SummaryWriterCache.get(self.cfg.save_path)
            tf_config = tf.ConfigProto(allow_soft_placement=True)
            sess_creator = ChiefSessionCreator(
                config=tf_config,
                checkpoint_dir=self.cfg.restore_path)
            self._hooks = self.get_hooks()
            sess_gen = MonitoredSession(session_creator=sess_creator,
                                        hooks=self._hooks)

            return sess_gen

    def _main_loop(self):

        self.experiment_begin()

        while not self.sess.should_stop():
            self.epoch_begin()

            for batch_id in range(self.train.nbatches):
                self.batch_begin()
                self.batch_do()
                self.batch_end()

            self.epoch_end()

        self.experiment_end()
        return self.return_value

    def get_train_dicts(self, which_op):
        train_dict = {
            'avg_loss': self.avg_loss[True]['train'],
            'train_op': self.train_graph_outs['grad_ops'][which_op]}
        train_summary_dict = {
            'avg_loss': self.avg_loss[True]['train'],
            'train_op': self.train_graph_outs['grad_ops'][which_op],
            'summary_op': self.train_graph_outs['summary_ops'][which_op]}
        return train_dict, train_summary_dict

    # ###########
    # Callbacks #
    # ###########
    def experiment_begin(self):
        # Add TqdmHandler
        handler = TqdmHandler()
        handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT, None))
        logger = logging.getLogger('tensorflow')
        del(logger.handlers[0])  # Remove the default handler
        logger.addHandler(handler)

        tf.logging.info('\nTrain dataset params:\n{}\n'.format(
            self.cfg.dataset_params))
        tf.logging.info('Validation dataset params:\n{}\n\n'.format(
            self.cfg.valid_params))

        # TODO find a better name?
        self.train = self.Dataset(
            which_set='train',
            return_list=False,
            **self.cfg.dataset_params)

        # Dump parameters and commit hash/diff to save path
        # Do not overwrite by default
        self._cfg_dump_dict['train_nbatches'] = self.train.nbatches
        self._cfg_dump_dict['train_nsamples'] = self.train.nsamples
        with open(self.cfg.cfg_dump_path, 'w') as f:
            f.write(json.dumps(self._cfg_dump_dict, sort_keys=True, indent=4,
                               separators=(',', ': ')))

        # Start the training loop
        self.start = time()
        tf.logging.info("Beginning main loop...")
        self.loss_value = 0
        self.global_step_val = self.global_step.eval(self.sess)

        # If it's the first run, log the hyperparameters in TB
        if (self.cfg.hyperparams_summaries is not None and
                self.global_step_val == 0):
            # write Hyper parameters text summaries
            summary = self.sess.run(self.summary_text_op)
            self.summary_writer.add_summary(summary, 0)

    def epoch_begin(self):
        self.epoch_id = self.global_step_val // self.train.nbatches

        summary_val = tf.Summary.Value(tag='T.control_flow/epoch',
                                       simple_value=self.epoch_id + 1)
        summary = tf.Summary(value=[summary_val])
        self.summary_writer.add_summary(summary, self.epoch_id)
        self.pbar = tqdm(total=self.train.nbatches,
                         bar_format='{n_fmt}/{total_fmt}{desc}'
                                    '{percentage:3.0f}%|{bar}| '
                                    '[{elapsed}<{remaining},'
                                    '{rate_fmt}{postfix}]')

    def batch_begin(self):
        iter_start = time()
        self._minibatch = self.train.next()
        self._t_data_load = time() - iter_start
        if self._t_data_load > 1:
            tf.logging.info('Data preprocess and loading took {}'
                            's. Consider increasing the '
                            'data_queues_size parameter.'.format(
                                self._t_data_load))

    def get_feed_dict(self, n_splits):
        # Get the per-device inputs
        minibatch_chunks = split_in_chunks(self._minibatch, n_splits,
                                           flatten_keys=['labels'])

        # Associate each placeholder (of each device) with its input data. Note
        # that the data is split in chunk, one per device. If this_n_splits is
        # smaller than the number of devices, the placeholders of the "extra"
        # devices are filled with the data of the first chunk. This is
        # necessary to feed the graph with the expected number of inputs, but
        # note that the extra outputs and loss will be ignored (see comment
        # where placeholders are created)
        feed_dict = {}
        for p_dict, batch_dict in zip_longest(self.per_dev_placeholders[True],
                                              minibatch_chunks,
                                              fillvalue=minibatch_chunks[0]):
            for p_name, p_obj in p_dict.iteritems():
                feed_dict[p_obj] = batch_dict[p_name]

        self._minibatch_chunks = minibatch_chunks
        # Extend the user-defined placeholders with those needed by the
        # main loop
        feed_dict[self.sym_num_devs] = n_splits
        feed_dict[self.sym_num_batches] = len(self._minibatch['data'])
        feed_dict[self.sym_prev_err] = self.loss_value
        return feed_dict

    def batch_do(self):
        cfg = self.cfg

        # inputs and labels
        x_batch = self._minibatch['data']
        # sh = inputs.shape  # do NOT provide a list of shapes

        # Is this batch shorter than batch_size?
        # Check if this batch will not be processed by all the devices.
        # When the sequence is shorter than seq_length or the number of
        # batches is smaller than batch_size, the batch will be smaller
        # than usual. When this happens we might not be able to feed
        # all the CPUs/GPUs altogether. In that case here we compute
        # the number of GPUs that we can use for the current batch
        # Spread the batch over the lowest number of GPUs
        this_n_splits = len(x_batch) // cfg.batch_size
        if len(x_batch) % cfg.batch_size != 0:
            this_n_splits += 1

        self._feed_dict = self.get_feed_dict(this_n_splits)

        # Use the op for the number of devices the current batch can feed
        which_op = this_n_splits - 1

        train_dict, train_summary_dict = self.get_train_dicts(which_op)

        # Compute (summaries and) loss
        # TODO make this a hook
        if self.global_step_val % self.cfg.train_summary_freq == 0:
            fetch_dict = self.sess.run(train_summary_dict,
                                       feed_dict=self._feed_dict)
            self.summary_writer.add_summary(fetch_dict['summary_op'],
                                            self.global_step_val)
        else:
            fetch_dict = self.sess.run(train_dict, feed_dict=self._feed_dict)
        self._fetch_dict = fetch_dict

        # Update self.loss_value, potentially used to decide the amount
        # of gradient noise in `process_gradients` via sym_prev_err.
        self.loss_value = fetch_dict['avg_loss']

    def batch_end(self):
        self.pbar.set_description('({:3d}) Ep {:d}'.format(
            self.global_step_val + 1, self.epoch_id + 1))
        avg_loss = self._fetch_dict['avg_loss']
        self.pbar.set_postfix({'D': '{:.2f}s'.format(self._t_data_load),
                               'loss': '{:.3f}'.format(avg_loss)})
        self.pbar.update(1)
        self.global_step_val += 1

    def epoch_end(self):
        self.pbar.close()
        for hook in self._hooks:
            if getattr(hook, '__name__', '') == 'EarlyStopHook':
                hook.val_skip -= 1

    def experiment_end(self):
        end = time()
        m, s = divmod(end - self.start, 60)
        h, m = divmod(m, 60)
        tf.logging.info("Total time elapsed: %d:%02d:%02d" % (h, m, s))

    # Inherit the method defined in optimization.py
    compute_and_process_grads = compute_and_process_grads
