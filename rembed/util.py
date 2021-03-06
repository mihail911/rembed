from collections import OrderedDict
import cPickle
import itertools
import math
import random

import numpy as np
import theano
from theano import tensor as T

numpy_random = np.random.RandomState(1234)
theano_random = T.shared_randomstreams.RandomStreams(numpy_random.randint(999999))

# With loaded embedding matrix, the padding vector will be initialized to zero
# and will not be trained. Hopefully this isn't a problem. It seems better than
# random initialization...
PADDING_TOKEN = "*PADDING*"

# Temporary hack: Map UNK to "_" when loading pretrained embedding matrices:
# it's a common token that is pretrained, but shouldn't look like any content words.
UNK_TOKEN = "_"

CORE_VOCABULARY = {PADDING_TOKEN: 0,
                   UNK_TOKEN: 1}

# Allowed number of transition types : currently PUSH : 0 and MERGE : 1
NUM_TRANSITION_TYPES = 2


def UniformInitializer(range):
    return lambda shape: np.random.uniform(-range, range, shape)



def HeKaimingInitializer():
    return lambda shape: np.random.normal(scale=math.sqrt(4.0/(shape[0] + shape[1])), size=shape)


def NormalInitializer(std):
    return lambda shape: np.random.normal(0.0, std, shape)


def ZeroInitializer():
    return lambda shape: np.zeros(shape)


def OneInitializer():
    return lambda shape: np.ones(shape)


def TreeLSTMBiasInitializer():
    def init(shape):
        hidden_dim = shape[0] / 5
        value = np.zeros(shape)
        value[hidden_dim:3*hidden_dim] = 1
        return value
    return init


def LSTMBiasInitializer():
    def init(shape):
        hidden_dim = shape[0] / 4
        value = np.zeros(shape)
        value[hidden_dim:2*hidden_dim] = 1
        return value
    return init


def DoubleIdentityInitializer(range):
    def init(shape):
        half_d = shape[0] / 2
        double_identity = np.concatenate((
            np.identity(half_d), np.identity(half_d)))
        return double_identity + UniformInitializer(range)(shape)
    return init


def BatchNorm(x, input_dim, vs, name, training_mode, axes=[0], momentum=0.9):
    """Apply simple batch normalization.
    This requires introducing new learned scale parameters, so it's 
    important to use unique names unless you're sure you want to share 
    these parameters.
    """

    # Create the trained gamma and beta parameters.
    g = vs.add_param("%s_bn_g" % name, (input_dim), 
        initializer=OneInitializer())
    b = vs.add_param("%s_bn_b" % name, (input_dim), 
        initializer=ZeroInitializer())

    # Create the training set moving averages for test time use.
    tracking_std = vs.add_param("%s_bn_ts" % name, (input_dim), 
        initializer=OneInitializer(),
        trainable=False)
    tracking_mean = vs.add_param("%s_bn_tm" % name, (input_dim), 
        initializer=ZeroInitializer(),
        trainable=False)

    # Compute the empirical mean and std.
    mean = x.mean(axis=axes, keepdims=True)
    std = T.sqrt(x.var(axis=axes, keepdims=True) + 1e-12)

    # Update the moving averages.
    vs.add_nongradient_update(tracking_std, (momentum * tracking_std + (1 - momentum) * std).flatten(ndim=1))
    vs.add_nongradient_update(tracking_mean, (momentum * tracking_mean + (1 - momentum) * mean).flatten(ndim=1))

    # Switch between train and test modes.
    effective_mean = mean * training_mode + tracking_mean * (1 - training_mode)
    effective_std = std * training_mode + tracking_std * (1 - training_mode)

    # Apply batch norm.
    return (x - effective_mean) * (g / effective_std) + b


class VariableStore(object):

    def __init__(self, prefix="vs", default_initializer=HeKaimingInitializer(), logger=None):
        self.prefix = prefix
        self.default_initializer = default_initializer
        self.vars = OrderedDict()  # Order is used in saving and loading
        self.savable_vars = OrderedDict()
        self.trainable_vars = OrderedDict()
        self.logger = logger
        self.nongradient_updates = OrderedDict()

    def add_param(self, name, shape, initializer=None, savable=True, trainable=True):
        if not initializer:
            initializer = self.default_initializer

        if name not in self.vars:
            full_name = "%s/%s" % (self.prefix, name)
            if self.logger:
                self.logger.Log(
                    "Created variable " + full_name + " shape: " + str(shape), level=self.logger.DEBUG)
            init_value = initializer(shape).astype(theano.config.floatX)
            self.vars[name] = theano.shared(init_value,
                                            name=full_name)
            if savable:
                self.savable_vars[name] = self.vars[name]
            if trainable:
                self.trainable_vars[name] = self.vars[name]

        return self.vars[name]

    def save_checkpoint(self, filename="vs_ckpt", keys=None, extra_vars=[]):
        if not keys:
            keys = self.savable_vars
        save_file = open(filename, 'w')  # this will overwrite current contents
        for key in keys:
            if self.logger:
                full_name = "%s/%s" % (self.prefix, key)
                self.logger.Log(
                    "Saving variable " + full_name, level=self.logger.DEBUG)
            cPickle.dump(self.vars[key].get_value(borrow=True), save_file, -1)  # the -1 is for HIGHEST_PROTOCOL
        for var in extra_vars:
            cPickle.dump(var, save_file, -1)
        save_file.close()

    def load_checkpoint(self, filename="vs_ckpt", keys=None, num_extra_vars=0, skip_saved_unsavables=False):
        if skip_saved_unsavables:
            keys = self.vars
        else:
            if not keys:
                keys = self.savable_vars
        save_file = open(filename)
        for key in keys:
            if skip_saved_unsavables and key not in self.savable_vars:
                if self.logger:
                    full_name = "%s/%s" % (self.prefix, key)
                    self.logger.Log(
                        "Not restoring variable " + full_name, level=self.logger.DEBUG)
                _ = cPickle.load(save_file) # Discard
            else:
                if self.logger:
                    full_name = "%s/%s" % (self.prefix, key)
                    self.logger.Log(
                        "Restoring variable " + full_name, level=self.logger.DEBUG)
                self.vars[key].set_value(cPickle.load(save_file), borrow=True)

        extra_vars = []
        for _ in range(num_extra_vars):
            extra_vars.append(cPickle.load(save_file))
        return extra_vars

    def add_nongradient_update(self, variable, new_value):
        # Track an update that should be applied during training but that aren't gradients.
        # self.nongradient_updates should be fed as an update to theano.function().
        self.nongradient_updates[variable] = new_value


def ReLULayer(inp, inp_dim, outp_dim, vs, name="relu_layer", use_bias=True, initializer=None):
    pre_nl = Linear(inp, inp_dim, outp_dim, vs, name, use_bias, initializer)
    # ReLU isn't present in this version of Theano.
    outp = T.maximum(pre_nl, 0)

    return outp


def Linear(inp, inp_dim, outp_dim, vs, name="linear_layer", use_bias=True, initializer=None):
    W = vs.add_param("%s_W" %
                     name, (inp_dim, outp_dim), initializer=initializer)
    outp = inp.dot(W)

    if use_bias:
        b = vs.add_param("%s_b" % name, (outp_dim,),
                         initializer=ZeroInitializer())
        outp += b

    return outp


def Dropout(inp, keep_rate, apply_dropout):
    """Apply dropout to a set of activations.

    Args:
      inp: Input vector.
      keep_rate: Dropout parameter. 1.0 entails no dropout.
      apply_dropout: A Theano scalar indicating whether to apply dropout (1.0)
        or eval-mode rescaling (0.0).
    """
    # TODO(SB): Investigate whether a Theano conditional would be faster than the linear combination below.

    dropout_mask = theano_random.binomial(n=1, p=keep_rate, size=inp.shape, dtype=theano.config.floatX)

    dropout_candidate = dropout_mask * inp 
    rescaling_candidate = keep_rate * inp
    result = apply_dropout * dropout_candidate + (1 - apply_dropout) * rescaling_candidate

    return result



def IdentityLayer(inp, inp_dim, outp_dim, vs, name="identity_layer", use_bias=True, initializer=None):
    """An identity function that takes the same parameters as the above layers."""
    assert inp_dim == outp_dim, "Identity layer requires inp_dim == outp_dim."
    return inp


def TreeLSTMLayer(lstm_prev, external_state, full_memory_dim, vs, name="tree_lstm", initializer=None, external_state_dim=0):
    assert full_memory_dim % 2 == 0, "Input is concatenated (h, c); dim must be even."
    hidden_dim = full_memory_dim / 2

    W = vs.add_param("%s/W" % name, (hidden_dim * 2 + external_state_dim, hidden_dim * 5),
                     initializer=initializer)
    b = vs.add_param("%s/b" % name, (hidden_dim * 5,),
                     initializer=TreeLSTMBiasInitializer())

    def slice_gate(gate_data, i):
        return gate_data[:, i * hidden_dim:(i + 1) * hidden_dim]

    # Decompose previous LSTM value into hidden and cell value
    l_h_prev = lstm_prev[:, :hidden_dim]
    l_c_prev = lstm_prev[:, hidden_dim:2 * hidden_dim]
    r_h_prev = lstm_prev[:, 2 * hidden_dim:3 * hidden_dim]
    r_c_prev = lstm_prev[:, 3 * hidden_dim:]
    if external_state_dim == 0:
        h_prev = T.concatenate([l_h_prev, r_h_prev], axis=1)
    else:
        h_prev = T.concatenate([l_h_prev, external_state, r_h_prev], axis=1)

    # Compute and slice gate values
    gates = T.dot(h_prev, W) + b
    i_gate, fl_gate, fr_gate, o_gate, cell_inp = [slice_gate(gates, i) for i in range(5)]

    # Apply nonlinearities
    i_gate = T.nnet.sigmoid(i_gate)
    fl_gate = T.nnet.sigmoid(fl_gate)
    fr_gate = T.nnet.sigmoid(fr_gate) 
    o_gate = T.nnet.sigmoid(o_gate)
    cell_inp = T.tanh(cell_inp)

    # Compute new cell and hidden value
    c_t = fl_gate * l_c_prev + fr_gate * r_c_prev + i_gate * cell_inp
    h_t = o_gate * T.tanh(c_t)

    return T.concatenate([h_t, c_t], axis=1)



def LSTMLayer(lstm_prev, inp, inp_dim, full_memory_dim, vs, name="lstm", initializer=None):
    assert full_memory_dim % 2 == 0, "Input is concatenated (h, c); dim must be even."
    hidden_dim = full_memory_dim / 2

    # input -> hidden mapping
    W = vs.add_param("%s_W" % name, (inp_dim, hidden_dim * 4), initializer=initializer)
    # hidden -> hidden mapping
    U = vs.add_param("%s_U" % name, (hidden_dim, hidden_dim * 4), initializer=initializer)
    # gate biases
    # TODO(jgauthier): support excluding params from regularization
    b = vs.add_param("%s_b" % name, (hidden_dim * 4,),
                     initializer=LSTMBiasInitializer())

    def slice_gate(gate_data, i):
        return gate_data[:, i * hidden_dim:(i + 1) * hidden_dim]

    # Decompose previous LSTM value into hidden and cell value
    h_prev = lstm_prev[:, :hidden_dim]
    c_prev = lstm_prev[:, hidden_dim:]

    # Compute and slice gate values
    gates = T.dot(inp, W) + T.dot(h_prev, U) + b
    i_gate, f_gate, o_gate, cell_inp = [slice_gate(gates, i) for i in range(4)]

    # Apply nonlinearities
    i_gate = T.nnet.sigmoid(i_gate)
    f_gate = T.nnet.sigmoid(f_gate)
    o_gate = T.nnet.sigmoid(o_gate)
    cell_inp = T.tanh(cell_inp)

    # Compute new cell and hidden value
    c_t = f_gate * c_prev + i_gate * cell_inp
    h_t = o_gate * T.tanh(c_t)

    return T.concatenate([h_t, c_t], axis=1)

def TrackingUnit(state_prev, inp, inp_dim, hidden_dim, vs, name="track_unit", make_logits=True):
    # Pass previous state and input to an LSTM layer.
    state = LSTMLayer(state_prev, inp, inp_dim, 2 * hidden_dim, vs, name="%s/lstm" % name)

    if make_logits:
        # Pass LSTM states through a Linear layer to predict the next transition.
        logits = Linear(state, 2 * hidden_dim, NUM_TRANSITION_TYPES, vs, name="%s/linear" % name)
    else:
        logits = 0.0

    return state, logits

def MLP(inp, inp_dim, outp_dim, vs, layer=ReLULayer, hidden_dims=None,
        name="mlp", initializer=None):
    if hidden_dims is None:
        hidden_dims = []

    prev_val = inp
    dims = [inp_dim] + hidden_dims + [outp_dim]
    for i, (src_dim, tgt_dim) in enumerate(zip(dims, dims[1:])):
        prev_val = layer(prev_val, src_dim, tgt_dim, vs, 
                         use_bias=True,
                         name="%s/%i" % (name, i),
                         initializer=initializer)
    return prev_val


def SGD(cost, params, lr=0.01):
    grads = T.grad(cost, params)

    new_values = OrderedDict()
    for param, grad in zip(params, grads):
        new_values[param] = param - lr * grad

    return new_values


def EmbeddingSGD(cost, embedding_matrix, lr=0.01, used_embeddings=None):
    new_values = OrderedDict()

    if used_embeddings:
        grads = T.grad(cost, wrt=used_embeddings)
        new_value = (used_embeddings,
                     T.inc_subtensor(used_embeddings, -lr * grads))
    else:
        new_values = SGD(cost, [embedding_matrix], lr)
        new_value = (embedding_matrix, new_values[embedding_matrix])

    return new_value


def Momentum(cost, params, lr=0.01, momentum=0.9):
    grads = T.grad(cost, params)

    new_values = OrderedDict()
    for param, grad in zip(params, grads):
        param_val = param.get_value(borrow=True)
        # momentum value
        m = theano.shared(np.zeros(param_val.shape, dtype=param_val.dtype))
        # compute velocity
        v = lr * grad + momentum * m

        new_values[m] = v
        new_values[param] = param - v

    return new_values


def RMSprop(cost, params, lr=0.001, rho=0.9, epsilon=1e-6):
    # From:
    # https://github.com/Newmu/Theano-Tutorials/blob/master/4_modern_net.py
    grads = T.grad(cost=cost, wrt=params)
    updates = []
    for p, g in zip(params, grads):
        acc = theano.shared(p.get_value() * 0.)
        acc_new = rho * acc + (1 - rho) * g ** 2
        gradient_scaling = T.sqrt(acc_new + epsilon)
        g = g / gradient_scaling
        updates.append((acc, acc_new))
        updates.append((p, p - lr * g))
    return updates


def TrimDataset(dataset, seq_length, eval_mode=False, sentence_pair_data=False):
    """Avoid using excessively long training examples."""
    if eval_mode:
        return dataset
    else:
        if sentence_pair_data:
            new_dataset = [example for example in dataset if
                len(example["premise_transitions"]) <= seq_length and 
                len(example["hypothesis_transitions"]) <= seq_length]   
        else:
            new_dataset = [example for example in dataset if len(
                example["transitions"]) <= seq_length]         
        return new_dataset


def TokensToIDs(vocabulary, dataset, sentence_pair_data=False):
    """Replace strings in original boolean dataset with token IDs."""
    if sentence_pair_data:
        keys = ["premise_tokens", "hypothesis_tokens"]
    else:
        keys = ["tokens"]

    for key in keys:
        if UNK_TOKEN in vocabulary:
            unk_id = vocabulary[UNK_TOKEN]
            for example in dataset:
                example[key] = [vocabulary.get(token, unk_id)
                                     for token in example[key]]
        else:
            for example in dataset:
                example[key] = [vocabulary[token]
                                for token in example[key]]
    return dataset


def CropAndPadExample(example, left_padding, target_length, key, logger=None):
    """
    Crop/pad a sequence value of the given dict `example`.
    """
    if left_padding < 0:
        # Crop, then pad normally.
        # TODO: Track how many sentences are cropped, but don't log a message
        # for every single one.
        example[key] = example[key][-left_padding:]
        left_padding = 0
    right_padding = target_length - (left_padding + len(example[key]))
    example[key] = ([0] * left_padding) + \
        example[key] + ([0] * right_padding)


def CropAndPad(dataset, length, logger=None, sentence_pair_data=False):
    # NOTE: This can probably be done faster in NumPy if it winds up making a
    # difference.
    # Always make sure that the transitions are aligned at the left edge, so
    # the final stack top is the root of the tree. If cropping is used, it should
    # just introduce empty nodes into the tree.
    if sentence_pair_data:
        keys = [("premise_transitions", "num_premise_transitions", "premise_tokens"), 
                ("hypothesis_transitions", "num_hypothesis_transitions", "hypothesis_tokens")]
    else:
        keys = [("transitions", "num_transitions", "tokens")]

    for example in dataset:
        for (transitions_key, num_transitions_key, tokens_key) in keys:
            example[num_transitions_key] = len(example[transitions_key])
            transitions_left_padding = length - example[num_transitions_key]
            shifts_before_crop_and_pad = example[transitions_key].count(0)
            CropAndPadExample(
                example, transitions_left_padding, length, transitions_key, logger=logger)
            shifts_after_crop_and_pad = example[transitions_key].count(0)
            tokens_left_padding = shifts_after_crop_and_pad - \
                shifts_before_crop_and_pad
            CropAndPadExample(
                example, tokens_left_padding, length, tokens_key, logger=logger)
    return dataset


def MakeTrainingIterator(sources, batch_size):
    # Make an iterator that exposes a dataset as random minibatches.

    def data_iter():
        dataset_size = len(sources[0])
        start = -1 * batch_size
        order = range(dataset_size)
        random.shuffle(order)

        while True:
            start += batch_size
            if start > dataset_size:
                # Start another epoch.
                start = 0
                random.shuffle(order)
            batch_indices = order[start:start + batch_size]
            yield tuple(source[batch_indices] for source in sources)
    return data_iter()


def MakeEvalIterator(sources, batch_size):
    # Make a list of minibatches from a dataset to use as an iterator.
    # TODO(SB): Handle the last few examples in the eval set if they don't
    # form a batch.

    dataset_size = len(sources[0])
    data_iter = []
    start = -batch_size
    while True:
        start += batch_size
        if start >= dataset_size:
            break
        data_iter.append(tuple(source[start:start + batch_size]
                               for source in sources))
    return data_iter


def PreprocessDataset(dataset, vocabulary, seq_length, data_manager, eval_mode=False, logger=None, 
                      sentence_pair_data=False):
    dataset = TrimDataset(dataset, seq_length, eval_mode=eval_mode, sentence_pair_data=sentence_pair_data)
    dataset = TokensToIDs(vocabulary, dataset, sentence_pair_data=sentence_pair_data)
    dataset = CropAndPad(dataset, seq_length, logger=logger, sentence_pair_data=sentence_pair_data)

    if sentence_pair_data:
        X = np.transpose(np.array([[example["premise_tokens"] for example in dataset],
                      [example["hypothesis_tokens"] for example in dataset]],
                     dtype=np.int32), (1, 2, 0))
        transitions = np.transpose(np.array([[example["premise_transitions"] for example in dataset],
                                [example["hypothesis_transitions"] for example in dataset]],
                               dtype=np.int32), (1, 2, 0))
        num_transitions = np.transpose(np.array(
            [[example["num_premise_transitions"] for example in dataset],
             [example["num_hypothesis_transitions"] for example in dataset]],
            dtype=np.int32), (1, 0))
    else:
        X = np.array([example["tokens"] for example in dataset],
                     dtype=np.int32)
        transitions = np.array([example["transitions"] for example in dataset],
                               dtype=np.int32)
        num_transitions = np.array(
            [example["num_transitions"] for example in dataset],
            dtype=np.int32)
    y = np.array(
        [data_manager.LABEL_MAP[example["label"]] for example in dataset],
        dtype=np.int32)

    return X, transitions, y, num_transitions


def BuildVocabulary(raw_training_data, raw_eval_sets, embedding_path, logger=None, sentence_pair_data=False):
    # Find the set of words that occur in the data.
    logger.Log("Constructing vocabulary...")
    types_in_data = set()
    for dataset in [raw_training_data] + [eval_dataset[1] for eval_dataset in raw_eval_sets]:
        if sentence_pair_data:
            types_in_data.update(itertools.chain.from_iterable([example["premise_tokens"]
                                                                for example in dataset]))
            types_in_data.update(itertools.chain.from_iterable([example["hypothesis_tokens"]
                                                                for example in dataset]))
        else:
            types_in_data.update(itertools.chain.from_iterable([example["tokens"]
                                                                for example in dataset]))
    logger.Log("Found " + str(len(types_in_data)) + " word types.")

    if embedding_path == None:
        logger.Log(
            "Warning: Open-vocabulary models require pretrained vectors. Running with empty vocabulary.")
        vocabulary = CORE_VOCABULARY
    else:
        # Build a vocabulary of words in the data for which we have an
        # embedding.
        vocabulary = BuildVocabularyForASCIIEmbeddingFile(
            embedding_path, types_in_data, CORE_VOCABULARY)

    return vocabulary


def BuildVocabularyForASCIIEmbeddingFile(path, types_in_data, core_vocabulary):
    """Quickly iterates through a GloVe-formatted ASCII vector file to
    extract a working vocabulary of words that occur both in the data and
    in the vector file."""

    # TODO(SB): Report on *which* words are skipped. See if any are common.

    vocabulary = {}
    vocabulary.update(core_vocabulary)
    next_index = len(vocabulary)
    with open(path, 'r') as f:
        for line in f:
            spl = line.split(" ", 1)
            word = spl[0]
            if word in types_in_data:
                vocabulary[word] = next_index
                next_index += 1
    return vocabulary


def LoadEmbeddingsFromASCII(vocabulary, embedding_dim, path):
    """Prepopulates a numpy embedding matrix indexed by vocabulary with
    values from a GloVe - format ASCII vector file.

    For now, values not found in the file will be set to zero."""
    emb = np.zeros(
        (len(vocabulary), embedding_dim), dtype=theano.config.floatX)
    with open(path, 'r') as f:
        for line in f:
            spl = line.split(" ")
            word = spl[0]
            if word in vocabulary:
                emb[vocabulary[word], :] = [float(e) for e in spl[1:]]
    return emb


def TransitionsToParse(transitions, words):
    stack = ["*ZEROS*"] * len(transitions)
    buffer_ptr = 0
    for transition in transitions:
        if transition == 0:
            stack.append("(P " + words[buffer_ptr] +")")
            buffer_ptr += 1
        elif transition == 1:
            r = stack.pop()
            l = stack.pop()
            stack.append("(M " + l + " " + r + ")")
    return stack.pop()
