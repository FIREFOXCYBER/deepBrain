import tensorflow as tf
import read_TFRec
import custom_RNN

NUM_CLASSES = read_TFRec.NUM_CLASSES

FLAGS = tf.app.flags.FLAGS

# Optional arguments for the model hyperparameters.

tf.app.flags.DEFINE_integer('patient', default_value=1,
                            docstring='''Patient number for which the model is built.''')
tf.app.flags.DEFINE_string('train_dir', default_value='.\\models\\train',
                           docstring='''Directory to write event logs and checkpoints.''')
tf.app.flags.DEFINE_string('data_dir', default_value='.\\data\\TFRecords\\',
                           docstring='''Path to the TFRecords.''')
tf.app.flags.DEFINE_integer('num_gpus', default_value=1,
                            docstring="""How many GPUs to use.""")
tf.app.flags.DEFINE_integer('max_steps', default_value=1000,
                           docstring='''Number of batches to run.''')
tf.app.flags.DEFINE_boolean('log_device_placement', default_value=False,
                           docstring='''Whether to log device placement.''')
tf.app.flags.DEFINE_integer('batch_size', default_value=2,
                           docstring='''Number of inputs to process in a batch.''')
tf.app.flags.DEFINE_integer('temporal_stride', default_value=2,
                           docstring='''Stride along time.''')
tf.app.flags.DEFINE_boolean('shuffle', default_value=True,
                           docstring='''Whether to shuffle or not the train data.''')
tf.app.flags.DEFINE_boolean('use_fp16', default_value=False,
                           docstring='''Type of data.''')
tf.app.flags.DEFINE_float('keep_prob', default_value=0.5,
                           docstring='''Keep probability for dropout.''')
tf.app.flags.DEFINE_integer('num_hidden', default_value=2048,
                           docstring='''Number of hidden nodes.''')
tf.app.flags.DEFINE_integer('num_conv_layers', default_value=1,
                           docstring='''Number of convolutional layers.''')
tf.app.flags.DEFINE_integer('num_rnn_layers', default_value=1,
                           docstring='''Number of recurrent layers.''')
tf.app.flags.DEFINE_string('checkpoint', default_value=None,
                           docstring='''Continue training from checkpoint file.''')
tf.app.flags.DEFINE_string('cell_type', default_value='LSTM',
                           docstring='''Type of cell to use for the recurrent layers.''')
tf.app.flags.DEFINE_string('rnn_type', default_value='uni-dir',
                           docstring='''uni-dir or bi-dir.''')
tf.app.flags.DEFINE_float('initial_lr', default_value=0.00001,
                           docstring='''Initial learning rate for training.''')
tf.app.flags.DEFINE_integer('num_filters', default_value=64,
                           docstring='''Number of convolutional filters.''')
tf.app.flags.DEFINE_float('moving_avg_decay', default_value=0.9999,
                           docstring='''Decay to use for the moving average of weights.''')
tf.app.flags.DEFINE_integer('num_epochs_per_decay', default_value=5,
                           docstring='''Epochs after which learning rate decays.''')
tf.app.flags.DEFINE_float('lr_decay_factor', default_value=0.9,
                           docstring='''Learning rate decay factor.''')

# Read architecture hyper-parameters from checkpoint file if one is provided.
if FLAGS.checkpoint is not None:
    param_file = FLAGS.checkpoint + '\\deepBrain_parameters.json'
    with open(param_file, 'r') as file:
        params = json.load(file)
        # Read network architecture parameters from previously saved
        # parameter file.
        FLAGS.num_hidden = params['num_hidden']
        FLAGS.num_rnn_layers = params['num_rnn_layers']
        FLAGS.rnn_type = params['rnn_type']
        FLAGS.num_filters = params['num_filters']
        FLAGS.use_fp16 = params['use_fp16']
        FLAGS.temporal_stride = params['temporal_stride']
        FLAGS.initial_lr = params['initial_lr']

from util import _variable
from util import _variable_with_weight_decay
from util import _activation_summary


# these values can be found by the log file generated by create_TFRec.py
if FLAGS.patient == 1:
    NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 306
    NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 38
elif FLAGS.patient == 2:
    NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 239
    NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 30
elif FLAGS.patient == 3:
    NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 260
    NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 32
elif FLAGS.patient == 4:
    NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 578
    NUM_EXAMPLES_PER_EPOCH_FOR_EVAL = 72

def inputs(eval_data, shuffle=False):
    """
    Construct input for the model evaluation using the Reader ops.

    :eval_data: 'train', 'test' or 'eval'
    :shuffle: bool, to shuffle the tfrecords or not. 
    
    :returns:
      feats: 3D tensor of [batch_size, T, CH] size.
      labels: Labels. 1D tensor of [batch_size] size.
      seq_lens: SeqLens. 1D tensor of [batch_size] size.

    :raises:
      ValueError: If no data_dir
    """
    if not FLAGS.data_dir:
        raise ValueError('Please supply a data_dir')
    tot_batch_size = FLAGS.batch_size * FLAGS.num_gpus
    feats, labels, seq_lens = read_TFRec.inputs(patientNr=FLAGS.patient,
                                                eval_data=eval_data,
                                                data_dir=FLAGS.data_dir,
                                                batch_size=tot_batch_size,
                                                shuffle=shuffle)
    if FLAGS.use_fp16:
        feats = tf.cast(feats, tf.float16)
    return feats, labels, seq_lens


def conv_layer(l_input, kernel_shape, scope):
    '''
    Convolutional layers wrapper function.

    :feats: input of conv layer
    :kernel_shape: shape of filter

    :returns:
       :conv_drop: tensor variable
       :kernel: tensor variable
    '''
    
    kernel = _variable_with_weight_decay(
        'weights',
        shape=kernel_shape,
        wd_value=None,
        use_fp16=FLAGS.use_fp16)

    conv = tf.nn.conv2d(l_input, kernel,
                        [1, FLAGS.temporal_stride, 1, 1],
                         padding='SAME')

    biases = _variable('biases', [FLAGS.num_filters],
                                tf.constant_initializer(-0.05),
                                FLAGS.use_fp16)
        
    bias = tf.nn.bias_add(conv, biases)
    conv = tf.nn.relu(bias, name=scope.name)
    _activation_summary(conv)

    # dropout
    conv_drop = tf.nn.dropout(conv, FLAGS.keep_prob)
    return conv_drop, kernel


def inference(feats, seq_lens):
    '''
    Build the deepBrain model.

    :feats: ECoG features returned from inputs().
    :seq_lens: Input sequence length for each utterance.

    :returns: logits.
    '''
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32

    feat_len = feats.get_shape().as_list()[-1]

    # expand the dimension of feats from [batch_size, T, CH] to [batch_size, T, CH, 1]
    feats = tf.expand_dims(feats, dim=-1)
    
    # convolutional layers
    with tf.variable_scope('conv1') as scope:
        conv_drop, kernel = conv_layer(l_input=feats,
                                       kernel_shape=[11, feat_len, 1, FLAGS.num_filters],
                                       scope=scope)

    if FLAGS.num_conv_layers > 1:
        for layer in range(2, FLAGS.num_conv_layers + 1):
            with tf.variable_scope('conv' + str(layer)) as scope:
                conv_drop, _ = conv_layer(l_input=conv_drop,
                                          kernel_shape=[11, feat_len, FLAGS.num_filters, FLAGS.num_filters],
                                          scope=scope)


    # recurrent layer
    with tf.variable_scope('rnn') as scope:

        # Reshape conv output to fit rnn input
        rnn_input = tf.reshape(conv_drop, [FLAGS.batch_size, -1, feat_len*FLAGS.num_filters])
        
        # Permute into time major order for rnn
        rnn_input = tf.transpose(rnn_input, perm=[1, 0, 2])
        
        # Make one instance of cell on a fixed device,
        # and use copies of the weights on other devices.
        if FLAGS.cell_type == 'LSTM':
            cell = tf.nn.rnn_cell.LSTMCell(FLAGS.num_hidden, activation=tf.nn.relu6)
        elif FLAGS.cell_type == 'CustomRNN':
            cell = custom_RNN.LayerNormalizedLSTMCell(FLAGS.num_hidden, activation=tf.nn.relu6, use_fp16=use_fp16)
            
        drop_cell = tf.nn.rnn_cell.DropoutWrapper(cell, output_keep_prob=FLAGS.keep_prob)
        multi_cell = tf.nn.rnn_cell.MultiRNNCell([drop_cell] * FLAGS.num_rnn_layers)

        seq_lens = tf.div(seq_lens, FLAGS.temporal_stride)
        if FLAGS.rnn_type == 'uni-dir':
            rnn_outputs, _ = tf.nn.dynamic_rnn(multi_cell, rnn_input,
                                               sequence_length=seq_lens,
                                               dtype=dtype, time_major=True, 
                                               scope='rnn')
        else:
            outputs, _ = tf.nn.bidirectional_dynamic_rnn(
                multi_cell, multi_cell, rnn_input,
                sequence_length=seq_lens, dtype=dtype,
                time_major=True, scope='rnn')
            outputs_fw, outputs_bw = outputs
            rnn_outputs = outputs_fw + outputs_bw
        _activation_summary(rnn_outputs)

    # Linear layer(WX + b) - softmax is applied by CTC cost function.
    with tf.variable_scope('fully_connected') as scope:
        weights = _variable_with_weight_decay(
            'weights', [FLAGS.num_hidden, NUM_CLASSES],
            wd_value=None,
            use_fp16=FLAGS.use_fp16)
        biases = _variable('biases', [NUM_CLASSES],
                                  tf.constant_initializer(0.0),
                                  FLAGS.use_fp16)
        logit_inputs = tf.reshape(rnn_outputs, [-1, cell.output_size])
        logits = tf.add(tf.matmul(logit_inputs, weights),
                        biases, name=scope.name)
        logits = tf.reshape(logits, [-1, FLAGS.batch_size, NUM_CLASSES])
        _activation_summary(logits)

    return logits

def loss(logits, labels, seq_lens):
    """Compute mean CTC Loss.
    Add summary for "Loss" and "Loss/avg".
    Args:
      logits: Logits from inference().
      labels: Labels from inputs(). 1-D tensor
              of shape [batch_size]
      seq_lens: Length of each utterance for ctc cost computation.
    Returns:
      Loss tensor of type float.
    """
    # Calculate the average ctc loss across the batch.
    ctc_loss = tf.nn.ctc_loss(inputs=tf.cast(logits, tf.float32),
                              labels=labels, sequence_length=seq_lens)
    ctc_loss_mean = tf.reduce_mean(ctc_loss, name='ctc_loss')
    tf.add_to_collection('losses', ctc_loss_mean)

    # The total loss is defined as the cross entropy loss plus all
    # of the weight decay terms (L2 loss).
    return tf.add_n(tf.get_collection('losses'), name='total_loss')
                            

def _add_loss_summaries(total_loss):
    """Add summaries for losses in deepBrain model.
    Generates moving average for all losses and associated summaries for
    visualizing the performance of the network.
    
    :total_loss: Total loss from loss().
    
    :returns:
      :loss_averages_op: op for generating moving averages of losses.
    """
    # Compute the moving average of all individual losses and the total loss.
    loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
    losses = tf.get_collection('losses')
    loss_averages_op = loss_averages.apply(losses + [total_loss])

    # Attach a scalar summary to all individual losses and the total loss;
    # do the same for the averaged version of the losses.
    for each_loss in losses + [total_loss]:
        # Name each loss as '(raw)' and name the moving average
        # version of the loss as the original loss name.
        tf.summary.scalar(each_loss.op.name + ' (raw)', each_loss)
        tf.summary.scalar(each_loss.op.name, loss_averages.average(each_loss))

    return loss_averages_op


def train(total_loss, global_step):
  """Train deepBrain model.
  Create an optimizer and apply to all trainable variables. Add moving
  average for all trainable variables.
  Args:
    total_loss: Total loss from loss().
    global_step: Integer Variable counting the number of training steps
      processed.
  Returns:
    train_op: op for training.
  """
  # Variables that affect learning rate.
  num_batches_per_epoch = NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / FLAGS.batch_size
  decay_steps = int(num_batches_per_epoch * FLAGS.num_epochs_per_decay)

  # Decay the learning rate exponentially based on the number of steps.
  lr = tf.train.exponential_decay(FLAGS.initial_lr,
                                  global_step,
                                  decay_steps,
                                  FLAGS.lr_decay_factor,
                                  staircase=True)
  tf.summary.scalar('learning_rate', lr)

  # Generate moving averages of all losses and associated summaries.
  loss_averages_op = _add_loss_summaries(total_loss)

  # Compute gradients.
  with tf.control_dependencies([loss_averages_op]):
    opt = tf.train.GradientDescentOptimizer(lr)
    grads = opt.compute_gradients(total_loss)

  # Apply gradients.
  apply_gradient_op = opt.apply_gradients(grads, global_step=global_step)

  # Add histograms for trainable variables.
  for var in tf.trainable_variables():
    tf.summary.histogram(var.op.name, var)

  # Add histograms for gradients.
  for grad, var in grads:
    if grad is not None:
      tf.summary.histogram(var.op.name + '/gradients', grad)

  # Track the moving averages of all trainable variables.
  variable_averages = tf.train.ExponentialMovingAverage(
      FLAGS.moving_avg_decay, global_step)
  variables_averages_op = variable_averages.apply(tf.trainable_variables())

  with tf.control_dependencies([apply_gradient_op, variables_averages_op]):
    train_op = tf.no_op(name='train')

  return train_op
