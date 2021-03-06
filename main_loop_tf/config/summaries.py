import gflags
from main_loop_tf import gflags_ext


# Summaries and samples
gflags.DEFINE_bool('show_samples_summaries', True, 'Whether to save the '
                   'GT/Prediction image summaries')
gflags.DEFINE_bool('show_heatmaps_summaries', True, 'Whether to save the '
                   'summaries of the heatmaps of the softmax distribution '
                   'per each class')
gflags.DEFINE_bool('save_gif_on_disk', False, 'Whether to save a GIF '
                   'animation of the video frames, their GT and the '
                   'prediction of the model. Note that the GIF generation is '
                   'not guaranteed to save the frames in the right order due '
                   'to parallelism')
gflags.DEFINE_bool('save_gif_frames_on_disk', False, 'Whether to save the '
                   'frames of the GIF as separate images on disk. This can '
                   'be useful because the GIF generation is not guaranteed to '
                   'save the frames in the right order due to parallelism')
gflags.DEFINE_bool('save_raw_predictions_on_disk', False, 'Whether to save '
                   'the predictions on disk as images. This is useful, e.g., '
                   'to send the predictions to an evaluation server')
gflags.DEFINE_bool('group_summaries', True, 'If True, groups the scalar '
                   'summaries by `layer_sublayer` rather than just by '
                   '`layer`. The total number of summaries remains unchanged')
gflags.DEFINE_integer('train_summary_freq', 10,
                      'How frequent save train summaries (in steps)')
gflags.DEFINE_integer('val_summary_freq', 10,
                      'How frequent save validation summaries (in steps)')
gflags.DEFINE_bool('summary_per_subset', False,
                   'If True mIoUs are saved per subset/video')
gflags_ext.DEFINE_multidict('hyperparams_summaries',
                            {'1-Dataset': ['dataset',
                                           'batch_size',
                                           'crop_size',
                                           'seq_length',
                                           'of',
                                           'remove_mean',
                                           'divide_by_std',
                                           'remove_per_img_mean',
                                           'divide_by_per_img_std'],
                             '2-Optimization': ['optimizer',
                                                'lr',
                                                'lr_decay',
                                                'decay_steps',
                                                'decay_rate',
                                                'staircase',
                                                'lr_boundaries',
                                                'lr_values',
                                                'power',
                                                'end_lr'],
                             '3-GradientProcessing': ['max_grad_norm',
                                                      'grad_noise_scale',
                                                      'grad_noise_decay',
                                                      'thresh_loss',
                                                      'grad_multiplier'],
                             '4-Regularization': ['weight_decay']
                             },
                            'Hyperparams you want to show in the summaries')
