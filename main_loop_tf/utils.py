import logging
import os
from subprocess import check_output
import sys
import tqdm

import gflags
import matplotlib
import numpy as np
import tensorflow as tf

# Initialize numpy's random seed
# import settings  # noqa


# Force matplotlib not to use any Xwindows backend.
matplotlib.use('Agg')
sys.setrecursionlimit(99999)
tf.logging.set_verbosity(tf.logging.INFO)


def split_in_chunks(minibatch, num_splits, flatten_keys=['labels']):
    '''Return the splits per device

    Return a list of dictionaries, one per device. Each dictionary
    contains, for each key, the values that should be allocated on its
    device.
    '''
    # Split the value of each key into chunks
    out = {}
    for k, v in minibatch.iteritems():
        out[k] = np.array_split(v.copy(), num_splits)
        if any(k == v for v in flatten_keys):
            out[k] = [el.flatten() for el in out[k]]
    return map(dict, zip(*[[(k, v) for v in value]
                           for k, value in out.items()]))


def apply_loss(labels, net_out, loss_fn, weight_decay, is_training,
               return_mean_loss=False, mask_voids=True):
    '''Applies the user-specified loss function and returns the loss

    Note:
        SoftmaxCrossEntropyWithLogits expects labels NOT to be one-hot
        and net_out to be one-hot.
    '''

    cfg = gflags.cfg

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

    if is_training:
        loss = apply_l2_penalty(loss, weight_decay)

    # Return the mean loss (over pixels *and* batches)
    if return_mean_loss:
        if mask_voids and len(cfg.void_labels):
            return tf.reduce_sum(loss) / tf.reduce_sum(mask)
        else:
            return tf.reduce_mean(loss)
    else:
        return loss


def apply_l2_penalty(loss, weight_decay):
    with tf.variable_scope('L2_regularization'):
        trainable_variables = tf.trainable_variables()
        l2_penalty = tf.add_n([tf.nn.l2_loss(v) for v in trainable_variables
                               if 'bias' not in v.name])
        loss += l2_penalty * weight_decay

    return loss


def save_repos_hash(params_dict, this_repo_name, packages=['theano']):
    # Repository hash and diff
    cwd = os.path.dirname(os.path.realpath(__file__))
    params_dict[this_repo_name + '_hash'] = check_output(
        'git rev-parse HEAD', cwd=cwd, shell=True)[:-1]
    diff = check_output('git diff', cwd=cwd, shell=True)
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
        scope_str = '.'.join([scope_str] + var_name.split('/')[:2])
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


def recursive_dict_stack(a_dict, a_target_dict):
    """Stack dictionaries values in lists

    Stack all the values returned of the first dictionary in a list in
    the second dictionary, so that the second dictionary contains the
    same keys but has a list as associated value, where the values of
    different calls are accumulated."""
    for k, v in a_dict.iteritems():
        if isinstance(v, dict):
            recursive_dict_stack(
                v, a_target_dict.setdefault(k, {}))
        else:
            a_target_dict.setdefault(k, []).append(v)


def recursive_truncate_dict(a_dict, sym_max_len, parent_k=None,
                            exact_len=None):
    """Truncate lists in (nested) dictionaries

    This function gets as an input a dictionary whose values are either
    lists or a nested dictionaries with the same property. The leafes of this
    structure are converted from from lists of tensors to concatenated tensors.
    These are finally truncated to be at most `sym_max_len` long.

    Parameters
    ----------
    a_dict: dictionary
        The dictionary to be truncated
    sym_max_len: Tensor or Placeholder or Variable
        The index of the truncation
    parent_k: string (optional)
        A string to be prepended to the key of the dict in the name of the op
    exact_len: int (optional)
        The number of elements that each list should have. If provided,
        the length of the lists will be checked.
    """
    ret_dict = {}
    for k, v in a_dict.iteritems():
        if isinstance(v, dict):
            k_list = '_'.join([parent_k, str(k)]) if parent_k else k
            ret_dict[k] = recursive_truncate_dict(v, sym_max_len, k_list,
                                                  exact_len=exact_len)
        else:
            if not isinstance(v, list):
                raise ValueError('The input should be a dictionary of lists')
            if exact_len:
                assert len(v) == exact_len, 'Key {} len: {}'.format(k, len(v))
            if len(v) == 1:
                # No need to concat if it's just one value
                ret_dict[k] = v[0]
            else:
                try:
                    tmp = tf.concat(v, axis=0, name='concat_%s' % str(k))
                except ValueError:
                    tmp = tf.stack(v, axis=0, name='stack_%s' % str(k))
                ret_dict[k] = tmp[:sym_max_len]
    return ret_dict


def uniquify_path(path, extension=''):
    """Adds an incremental suffix to the path until it's unique

    Parameters
    ----------
        path: string
            The path to be uniquified.
        extension: string, optional
            The extension of the file to be uniquified.

    Returns
    -------
        last_existing_path: string
            The existing path with the highest incremental suffix.
        unique_path: string
            A unique path generated adding an incremental suffix to the
            input path if necessary.
    """
    if extension != '':
        extension = '.' + extension
    incr_num = 0
    unique_path = path + extension
    last_existing_path = path + extension
    while(os.path.exists(unique_path)):
        incr_num += 1
        last_existing_path = unique_path
        unique_path = path + '_' + str(incr_num) + extension
    return last_existing_path, unique_path
