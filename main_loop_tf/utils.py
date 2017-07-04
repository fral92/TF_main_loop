import logging
from subprocess import check_output
import sys
import tqdm

import gflags
import matplotlib
import numpy as np
import tensorflow as tf
from tensorflow.contrib import slim
from tensorflow.contrib.layers.python.layers.optimizers import (
    _clip_gradients_by_norm,
    _add_scaled_noise_to_gradients,
    _multiply_gradients)
from tensorflow.python.ops import math_ops

# Initialize numpy's random seed
# import settings  # noqa


# Force matplotlib not to use any Xwindows backend.
matplotlib.use('Agg')
sys.setrecursionlimit(99999)
tf.logging.set_verbosity(tf.logging.INFO)


def split_in_chunks(x_batch, y_batch, gpus_used):
    '''Return the splits per gpu

    Return
        * the batches per gpu
        * the labels elements per gpu
    '''

    x_batch_chunks = np.array_split(x_batch, gpus_used)
    y_batch_chunks = np.array_split(y_batch, gpus_used)
    for i in range(gpus_used):
        y_batch_chunks[i] = y_batch_chunks[i].flatten()

    return x_batch_chunks, y_batch_chunks


def apply_loss(labels, net_out, loss_fn, weight_decay, is_training,
               return_mean_loss=False, mask_voids=True):
    '''Applies the user-specified loss function and returns the loss

    Note:
        SoftmaxCrossEntropyWithLogits expects labels NOT to be one-hot
        and net_out to be one-hot.
    '''

    cfg = gflags.cfg

    if cfg.of_prediction:
        net_out, of_preds = net_out

    if cfg.task in (cfg.task_names['seg'], cfg.task_names['class']):
        if mask_voids and len(cfg.void_labels):
            # TODO Check this
            print('Masking the void labels')
            mask = tf.not_equal(labels, cfg.void_labels)
            labels *= tf.cast(mask, 'int32')  # void_class --> 0 (random class)
            # Train loss
            loss = loss_fn(labels=labels,
                           logits=tf.reshape(net_out, [-1, cfg.nclasses]))
            mask = tf.cast(mask, 'float32')
            loss *= mask
        else:
            # Train loss
            loss = loss_fn(labels=labels,
                           logits=tf.reshape(net_out, [-1, cfg.nclasses]))
    else:
        if loss_fn is tf.losses.mean_squared_error:
            loss = loss_fn(labels=labels,
                           predictions=tf.reshape(net_out, [-1]))
        elif loss_fn is tf.nn.sigmoid_cross_entropy_with_logits:
            loss = loss_fn(labels=tf.cast(labels, cfg._FLOATX),
                           logits=tf.reshape(net_out, [-1]))
        # TODO: else statement

    if is_training:
        loss = apply_l2_penalty(loss, weight_decay)

    # Return the mean loss (over pixels *and* batches)
    if return_mean_loss:
        if mask_voids and len(cfg.void_labels):
            loss = tf.reduce_sum(loss) / tf.reduce_sum(mask)
        else:
            loss = tf.reduce_mean(loss)
        if cfg.of_regularization_type is not 'None':
            if not cfg.of_prediction:
                raise RuntimeError('The model does not perform optical'
                                   'flow prediction')
            else:
                if cfg.of_regularization_type == 'huber_penalty':
                    local_gradients = _compute_of_local_gradients(of_preds)
                    loss = apply_huber_penalty(loss, local_gradients)
                else:
                    raise NotImplementedError()
        return loss
    else:
        return loss


def apply_l2_penalty(loss, weight_decay):
    with tf.variable_scope('L2_regularization'):
        trainable_variables = tf.trainable_variables()
        l2_penalty = tf.add_n([tf.nn.l2_loss(v) for v in trainable_variables
                               if 'bias' not in v.name])
        loss += l2_penalty * weight_decay

    return loss


def _compute_of_local_gradients(of_preds):
    cfg = gflags.cfg

    if cfg.of_local_grads_filter == 'stn_stencil':
        s_filter_x = tf.constant_initializer(np.array(([0, 0, 0],
                                                       [-1.2, 0, 1.2],
                                                       [0, 0, 0]),
                                                      dtype='float32'))
        s_filter_y = tf.constant_initializer(np.array(([0, -1.2, 0],
                                                       [0, 0, 0],
                                                       [0, 1.2, 0]),
                                                      dtype='float32'))
    elif cfg.of_local_grads_filter == 'sobel':
        s_filter_x = tf.constant_initializer(np.array(([1, 0, -1],
                                                       [2, 0, -2],
                                                       [1, 0, -1]),
                                                      dtype='float32'))
        s_filter_y = tf.constant_initializer(np.array(([1, 2, 1],
                                                       [0, 0, 0],
                                                       [-1, -2, -1]),
                                                      dtype='float32'))
    else:
        raise NotImplementedError()

    with tf.variable_scope('of_local_gradients'):
        of_preds_x, of_preds_y = tf.split(of_preds, [1, 1], axis=3)
        with slim.arg_scope([slim.conv2d],
                            activation_fn=None,
                            trainable=False):
            grad_x = slim.conv2d(of_preds_x, 1, [3, 3], stride=1,
                                 weights_initializer=s_filter_x,
                                 normalizer_fn=None)
            grad_y = slim.conv2d(of_preds_y, 1, [3, 3], stride=1,
                                 weights_initializer=s_filter_y,
                                 normalizer_fn=None)
        grad_x = tf.square(grad_x)
        grad_y = tf.square(grad_y)
        local_gradients = tf.add(grad_x, grad_y)
        local_gradients = tf.sqrt(local_gradients)
    return local_gradients


def apply_huber_penalty(loss, gradients):
    cfg = gflags.cfg

    version = cfg.of_regularization_params['version']
    delta = cfg.of_regularization_params['delta']
    weight_decay = cfg.of_regularization_params['weight_decay']
    with tf.variable_scope('huber_penalty'):
        if version == 'custom':
            abs_grads = tf.abs(gradients)
            quadratic = 0.5 * tf.square(abs_grads)
            linear = delta * tf.subtract(abs_grads, 0.5 * delta)
            huber_penalty = weight_decay * tf.where(
                abs_grads <= delta, quadratic, linear)
        elif version == 'tf':
            abs_grads = math_ops.abs(gradients)
            quadratic = math_ops.minimum(abs_grads, delta)
            # The following expression is the same in value as
            # tf.maximum(abs_error - delta, 0), but importantly the gradient
            # for the expression when abs_error == delta is 0 (for
            # tf.maximum it would be # 1).
            # This is necessary to avoid doubling the gradient, since there is
            # already a nonzero contribution to the gradient from the quadratic
            # term.
            linear = (abs_grads - quadratic)
            huber_penalty = weight_decay * (
                0.5 * quadratic**2 + delta * linear)
        else:
            raise NotImplementedError()
        loss += tf.reduce_mean(huber_penalty)
    return loss


def process_gradients(gradients,
                      gradient_noise_scale=None,
                      gradient_multipliers=None,
                      clip_gradients=None):

    """
    gradient_noise_scale: float or None, adds 0-mean normal noise scaled
        by this value.
    gradient_multipliers: dict of variables or variable names to floats.
        If present, gradients for specified variables will be multiplied
        by given constant.
    clip_gradients: float, callable or `None`. If float, is provided, a global
      clipping is applied to prevent the norm of the gradient to exceed this
      value. Alternatively, a callable can be provided e.g.: adaptive_clipping.
      This callable takes a `list` of `(gradients, variables)` `tuple`s and
      returns the same thing with the gradients modified.
    """

    if gradient_noise_scale is not None:
        gradients = _add_scaled_noise_to_gradients(
            gradients, gradient_noise_scale)

    # Multiply some gradients.
    if gradient_multipliers is not None:
        gradients = _multiply_gradients(
            gradients, gradient_multipliers)
        if not gradients:
            raise ValueError(
                "Empty list of (gradient,var) pairs"
                "encountered. This is most likely "
                "to be caused by an improper value "
                "of gradient_multipliers.")

    # Optionally clip gradients by global norm.
    if isinstance(clip_gradients, float):
        gradients = _clip_gradients_by_norm(
            gradients, clip_gradients)
    elif callable(clip_gradients):
        gradients = clip_gradients(gradients)
    elif clip_gradients is not None:
        raise ValueError(
            "Unknown type %s for clip_gradients" % type(clip_gradients))

    return gradients


def average_gradients(tower_grads):
    """Calculate the average gradient for each shared variable across all towers.

    Note that this function provides a synchronization point across all towers.

    Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over individual gradients. The inner list is over the gradient
      calculation for each tower.
    Returns:
     List of pairs of (gradient, variable) where the gradient has been averaged
     across all towers.
    """
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        # Note that each grad_and_vars looks like the following:
        #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
        # TODO no need for the loop here
        # grad.append(mean(grad_gpu[0..N]), var_gpu0)
        grads = []
        for g, _ in grad_and_vars:
            # Add 0 dimension to the gradients to represent the tower.
            expanded_g = tf.expand_dims(g, 0)

            # Append on a 'tower' dimension which we will average over below.
            grads.append(expanded_g)

        # Average over the 'tower' dimension.
        grad = tf.concat(axis=0, values=grads)
        grad = tf.reduce_mean(grad, 0)

        # Keep in mind that the Variables are redundant because they are shared
        # across towers. So .. we will just return the first tower's pointer to
        # the Variable.
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)

    return average_grads


def save_repos_hash(params_dict, this_repo_name, packages=['theano']):
    # Repository hash and diff
    params_dict[this_repo_name + '_hash'] = check_output('git rev-parse HEAD',
                                                         shell=True)[:-1]
    diff = check_output('git diff', shell=True)
    if diff != '':
        params_dict[this_repo_name + '_diff'] = diff
    # packages
    for p in packages:
        this_pkg = __import__(p)
        params_dict[p + '_hash'] = this_pkg.__version__


def fig2array(fig):
    """Convert a Matplotlib figure to a 4D numpy array

    Params
    ------
    fig:
        A matplotlib figure

    Return
    ------
        A numpy 3D array of RGBA values

    Modified version of: http://www.icare.univ-lille1.fr/node/1141
    """
    # draw the renderer
    fig.canvas.draw()

    # Get the RGBA buffer from the figure
    w, h = fig.canvas.get_width_height()
    buf = np.fromstring(fig.canvas.tostring_rgb(), dtype=np.uint8)
    buf.shape = (h, w, 3)

    return buf


def squash_maybe(scope_str, var_name):
    cfg = gflags.cfg
    if cfg.group_summaries and var_name.count('/') >= 2:
        # Squash the first two levels into the name_scope
        # to merge the summaries that belong to the same
        # part of the model together in tensorboard
        scope_str = '_'.join([scope_str] + var_name.split('/')[:2])
        var_name = '/'.join(var_name.split('/')[2:])
    return scope_str, var_name


class TqdmHandler(logging.StreamHandler):
    # From https://github.com/tqdm/tqdm/issues/193#issuecomment-233212170
    def __init__(self):
        logging.StreamHandler.__init__(self)

    def emit(self, record):
        msg = self.format(record)
        tqdm.tqdm.write(msg)


def flowToColor(flow, varargin=None):
    '''
    Convert optical flow to RGB image

    From:
    https://github.com/stefanoalletto/TransFlow/blob/master/
    flowToColor.pyeadapted from
    '''
    # TODO: cleanup all the translator crap
    [height, widht, nBands] = flow.shape
    if nBands != 2.:
        np.error('flowToColor: image must have two bands')

    u = flow[:, :, 0]
    v = flow[:, :, 1]
    # print u.shape,v.shape
    maxu = -999.
    maxv = -999.
    minu = 999.
    minv = 999.
    maxrad = -1.
    # % fix unknown flow
    # idxUnknown = np.logical_or(np.abs(u) > UNKNOWN_FLOW_THRESH, np.abs(v) > UNKNOWN_FLOW_THRESH)
    # print np.array(idxUnknown)
    # u[int(idxUnknown)-1] = 0.
    # v[int(idxUnknown)-1] = 0.
    maxu = max(maxu, np.max(u))
    minu = max(minu, np.max(u))
    maxv = max(maxv, np.max(v))
    minv = max(minv, np.max(v))
    rad = np.sqrt((u ** 2. + v ** 2.))
    maxrad = max(maxrad, np.max(rad))
    # print 'max flow:',maxrad, ' flow range: u =', minu, maxu, 'v =', minv, maxv
    # if isempty(varargin) == 0.:
    #    maxFlow = varargin.cell[0]
    #    if maxFlow > 0.:
    #        maxrad = maxFlow
    u = u / (maxrad + 1e-5)
    v = v / (maxrad + 1e-5)
    # % compute color
    img = computeColor(u, v)
    # % unknown flow
    # IDX = np.repmat(idxUnknown, np.array(np.hstack((1., 1., 3.))))
    # img[int(IDX)-1] = 0.
    return img/255.


def computeColor(u, v):
    img = np.zeros((u.shape[0], u.shape[1], 3))
    # nanIdx = np.logical_or(np.isnan(u), np.isnan(v))
    # u[int(nanIdx)-1] = 0.
    # v[int(nanIdx)-1] = 0.
    colorwheel, ncols = makeColorwheel()
    rad = np.sqrt((u ** 2. + v ** 2.))
    a = np.arctan2((-v), (-u)) / np.pi
    fk = np.dot((a + 1.) / 2., ncols - 1.)
    # % -1~1 maped to 1~ncols
    k0 = np.floor(fk).astype(np.int32)
    # % 1, 2, ..., ncols
    k1 = k0 + 1
    k1[k1 == ncols] = 1
    f = fk - k0

    for i in np.arange(colorwheel.shape[-1]):
        tmp = colorwheel[:, i]
        col0 = tmp[k0] / 255.
        col1 = tmp[k1] / 255.
        col = (1. - f) * col0 + f * col1
        idx = rad <= 1.
        col[idx] = 1. - rad[idx] * (1. - col[idx])
        # % increase saturation with radius
        col[rad > 1] = col[rad > 1] * 0.75
        # % out of range
        img[:, :, i] = np.floor(255. * col)
    return img


def makeColorwheel():

    RY = 15
    YG = 6
    GC = 4
    CB = 11
    BM = 13
    MR = 6
    ncols = RY+YG+GC+CB+BM+MR
    colorwheel = np.zeros((int(ncols), 3))
    # % r g b
    col = 0
    # %RY
    colorwheel[0:RY, 0] = 255.
    colorwheel[0:RY, 1] = np.floor(255. * np.arange(0., RY) / RY)
    col = col + RY
    # %YG
    colorwheel[col:col+YG, 0] = 255. - np.floor(
        255. * np.arange(0., YG) / YG)
    colorwheel[col:col+YG, 1] = 255.
    col = col + YG
    # %GC
    colorwheel[col+0:col+GC, 1] = 255.
    colorwheel[col+0:col+GC, 2] = np.floor(255. * np.arange(0., GC) / GC)
    col = col + GC
    # %CB
    colorwheel[col+0:col+CB, 1] = 255. - np.floor(
        255. * np.arange(0., CB) / CB)
    colorwheel[col+0:col+CB, 2] = 255.
    col = col + CB
    # %BM
    colorwheel[col+0:col+BM, 2] = 255.
    colorwheel[col+0:col+BM, 0] = np.floor(255. * np.arange(0., BM) / BM)
    col = col + BM
    # %MR
    colorwheel[col+0:col+MR, 2] = 255. - np.floor(
        255. * np.arange(0., MR) / MR)
    colorwheel[col+0:col+MR, 0] = 255.
    return colorwheel, ncols
