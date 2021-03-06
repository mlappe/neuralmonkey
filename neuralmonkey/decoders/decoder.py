import math
from typing import cast, Iterable, List, Callable, Optional, Any, Tuple

import numpy as np
import tensorflow as tf
from typeguard import check_argument_types

from neuralmonkey.decoding_function import BaseAttention
from neuralmonkey.dataset import Dataset
from neuralmonkey.vocabulary import Vocabulary, START_TOKEN, END_TOKEN_INDEX
from neuralmonkey.model.model_part import ModelPart, FeedDict
from neuralmonkey.model.sequence import EmbeddedSequence
from neuralmonkey.logging import log, warn
from neuralmonkey.nn.ortho_gru_cell import OrthoGRUCell
from neuralmonkey.nn.utils import dropout
from neuralmonkey.encoders.attentive import Attentive
from neuralmonkey.nn.projection import linear
from neuralmonkey.decoders.encoder_projection import (
    linear_encoder_projection, concat_encoder_projection, empty_initial_state)
from neuralmonkey.decoders.output_projection import no_deep_output

RNN_CELL_TYPES = {
    "GRU": OrthoGRUCell,
    "LSTM": tf.contrib.rnn.LSTMCell
}


# pylint: disable=too-many-instance-attributes,too-few-public-methods
# Big decoder cannot be simpler. Not sure if refactoring
# it into smaller units would be helpful
class Decoder(ModelPart):
    """A class that manages parts of the computation graph that are
    used for the decoding.
    """

    # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    def __init__(self,
                 encoders: List[Any],
                 vocabulary: Vocabulary,
                 data_id: str,
                 name: str,
                 max_output_len: int,
                 dropout_keep_prob: float = 1.0,
                 rnn_size: Optional[int] = None,
                 embedding_size: Optional[int] = None,
                 output_projection: Optional[Callable[
                     [tf.Tensor, tf.Tensor, List[tf.Tensor]],
                     tf.Tensor]]=None,
                 encoder_projection: Optional[Callable[
                     [tf.Tensor, Optional[int], Optional[List[Any]]],
                     tf.Tensor]]=None,
                 use_attention: bool = False,
                 embeddings_source: Optional[EmbeddedSequence] = None,
                 attention_on_input: bool = True,
                 rnn_cell: str = 'GRU',
                 conditional_gru: bool = False,
                 save_checkpoint: Optional[str] = None,
                 load_checkpoint: Optional[str] = None) -> None:
        """Create a refactored version of monster decoder.

        Arguments:
            encoders: Input encoders of the decoder
            vocabulary: Target vocabulary
            data_id: Target data series
            name: Name of the decoder. Should be unique accross all Neural
                Monkey objects
            max_output_len: Maximum length of an output sequence
            dropout_keep_prob: Probability of keeping a value during dropout

        Keyword arguments:
            rnn_size: Size of the decoder hidden state, if None set
                according to encoders.
            embedding_size: Size of embedding vectors for target words
            output_projection: How to generate distribution over vocabulary
                from decoder rnn_outputs
            encoder_projection: How to construct initial state from encoders
            use_attention: Flag whether to look at attention vectors of the
                encoders
            embeddings_source: Embedded sequence to take embeddings from
            rnn_cell: RNN Cell used by the decoder (GRU or LSTM)
            conditional_gru: Flag whether to use the Conditional GRU
                architecture
            attention_on_input: Flag whether attention from previous decoding
                step should be combined with the input in the next step.
        """
        ModelPart.__init__(self, name, save_checkpoint, load_checkpoint)
        check_argument_types()

        log("Initializing decoder, name: '{}'".format(name))

        self.encoders = encoders
        self.vocabulary = vocabulary
        self.data_id = data_id
        self.max_output_len = max_output_len
        self.dropout_keep_prob = dropout_keep_prob
        self.embedding_size = embedding_size
        self.rnn_size = rnn_size
        self.output_projection = output_projection
        self.encoder_projection = encoder_projection
        self.use_attention = use_attention
        self.embeddings_source = embeddings_source
        self._conditional_gru = conditional_gru
        self._attention_on_input = attention_on_input
        self._rnn_cell_str = rnn_cell

        if self.embedding_size is None and self.embeddings_source is None:
            raise ValueError("You must specify either embedding size or the "
                             "embedded sequence from which to reuse the "
                             "embeddings (e.g. set either 'embedding_size' or "
                             " 'embeddings_source' parameter)")

        if self.embeddings_source is not None:
            if self.embedding_size is not None:
                warn("Overriding the embedding_size parameter with the"
                     " size of the reused embeddings from the encoder.")

            self.embedding_size = (
                self.embeddings_source.embedding_matrix.get_shape()[1].value)

        if self.encoder_projection is None:
            if not self.encoders:
                log("No encoder - language model only.")
                self.encoder_projection = empty_initial_state
            elif rnn_size is None:
                log("No rnn_size or encoder_projection: Using concatenation of"
                    " encoded states")
                self.encoder_projection = concat_encoder_projection
                self.rnn_size = sum(e.encoded.get_shape()[1].value
                                    for e in encoders)
            else:
                log("Using linear projection of encoders as the initial state")
                self.encoder_projection = linear_encoder_projection(
                    self.dropout_keep_prob)

        assert self.rnn_size is not None

        if self._rnn_cell_str not in RNN_CELL_TYPES:
            raise ValueError("RNN cell must be a either 'GRU' or 'LSTM'")

        if self.output_projection is None:
            log("No output projection specified - using simple concatenation")
            self.output_projection = no_deep_output

        with self.use_scope():
            with tf.variable_scope("attention_decoder") as self.step_scope:
                pass

            self._create_input_placeholders()
            self._create_training_placeholders()
            self._create_initial_state()
            self._create_embedding_matrix()

            with tf.name_scope("output_projection"):
                self.decoding_w = tf.get_variable(
                    "state_to_word_W", [self.rnn_size, len(self.vocabulary)],
                    initializer=tf.random_uniform_initializer(-0.5, 0.5))

                self.decoding_b = tf.get_variable(
                    "state_to_word_b", [len(self.vocabulary)],
                    initializer=tf.constant_initializer(
                        - math.log(len(self.vocabulary))))

            # POSLEDNI TRAIN INPUT SE V DEKODOVACI FUNKCI NEPOUZIJE
            # (jen jako target)
            embedded_train_inputs = self.embed_and_dropout(
                self.train_inputs[:-1])

            # POZOR TADY SE NEDELA DROPOUT
            embedded_go_symbols = tf.nn.embedding_lookup(self.embedding_matrix,
                                                         self.go_symbols)

            # fetch train attention objects
            self._train_attention_objects = {}
            # type: Dict[Attentive, tf.Tensor]
            if self.use_attention:
                with tf.name_scope("attention_object"):
                    self._train_attention_objects = {
                        e: e.create_attention_object()
                        for e in self.encoders
                        if isinstance(e, Attentive)}

            self.train_logits, _, _ = self._decoding_loop(
                embedded_go_symbols,
                train_inputs=embedded_train_inputs,
                train_mode=True)

            assert not tf.get_variable_scope().reuse
            tf.get_variable_scope().reuse_variables()

            # fetch runtime attention objects
            self._runtime_attention_objects = {}
            # type: Dict[Attentive, tf.Tensor]
            if self.use_attention:
                self._runtime_attention_objects = {
                    e: e.create_attention_object()
                    for e in self.encoders
                    if isinstance(e, Attentive)}

            (self.runtime_logits,
             self.runtime_rnn_states,
             self.runtime_mask) = self._decoding_loop(
                 embedded_go_symbols,
                 train_mode=False)

            train_targets = tf.transpose(self.train_inputs)

            self.train_xents = tf.contrib.seq2seq.sequence_loss(
                tf.stack(self.train_logits, 1), train_targets,
                tf.transpose(self.train_padding),
                average_across_batch=False)
            self.train_loss = tf.reduce_mean(self.train_xents)
            self.cost = self.train_loss

            self.train_logprobs = [tf.nn.log_softmax(l)
                                   for l in self.train_logits]

            self.decoded = [tf.argmax(logit[:, 1:], 1) + 1 for logit in
                            self.runtime_logits]

            self.runtime_loss = tf.contrib.seq2seq.sequence_loss(
                tf.stack(self.runtime_logits, 1), train_targets,
                tf.transpose(self.train_padding))

            self.runtime_logprobs = [tf.nn.log_softmax(l)
                                     for l in self.runtime_logits]

            self._visualize_attention()

            log("Decoder initalized.")
    # pylint: disable=too-many-arguments,too-many-locals,too-many-statements

    def _create_input_placeholders(self) -> None:
        """Creates input placeholder nodes in the computation graph"""
        self.train_mode = tf.placeholder(tf.bool, name="train_mode")

        self.go_symbols = tf.placeholder(tf.int32, shape=[1, None],
                                         name="decoder_go_symbols")

        self.batch_size = tf.shape(self.go_symbols)[1]

    def _create_training_placeholders(self) -> None:
        """Creates training placeholder nodes in the computation graph

        The training placeholder nodes are NOT fed during runtime.
        """
        self.train_inputs = tf.placeholder(
            tf.int32, [self.max_output_len, None],
            name="decoder_input_placeholder")

        self.train_padding = tf.placeholder(
            tf.float32, [self.max_output_len, None],
            name="decoder_padding_placeholder")

    def _create_initial_state(self) -> None:
        """Construct the part of the computation graph that computes
        the initial state of the decoder.
        """
        with tf.variable_scope("initial_state"):
            self.initial_state = dropout(
                self.encoder_projection(self.train_mode,
                                        self.rnn_size,
                                        self.encoders),
                self.dropout_keep_prob,
                self.train_mode)

            # pylint: disable=no-member
            # Pylint keeps complaining about initial shape being a tuple,
            # but it is a tensor!!!
            init_state_shape = self.initial_state.get_shape()
            # pylint: enable=no-member

            # Broadcast the initial state to the whole batch if needed
            if len(init_state_shape) == 1:
                assert init_state_shape[0].value == self.rnn_size
                tiles = tf.tile(self.initial_state,
                                tf.expand_dims(self.batch_size, 0))
                self.initial_state = tf.reshape(tiles, [-1, self.rnn_size])

    def _create_embedding_matrix(self) -> None:
        """Create variables and operations for embedding of input words

        If we are reusing word embeddings, this function takes the embedding
        matrix from the first encoder
        """
        if self.embeddings_source is None:
            # TODO better initialization
            self.embedding_matrix = tf.get_variable(
                "word_embeddings", [len(self.vocabulary), self.embedding_size],
                initializer=tf.random_uniform_initializer(-0.5, 0.5))
        else:
            self.embedding_matrix = self.embeddings_source.embedding_matrix

    def embed_and_dropout(self, inputs: tf.Tensor) -> tf.Tensor:
        """Embed the input using the embedding matrix and apply dropout

        Arguments:
            inputs: The Tensor to be embedded and dropped out.
        """
        with tf.variable_scope("embed_inputs"):
            embedded = tf.nn.embedding_lookup(
                self.embedding_matrix, inputs)
            return dropout(embedded,
                           self.dropout_keep_prob,
                           self.train_mode)

    def _logit_function(self, state: tf.Tensor) -> tf.Tensor:
        state = dropout(state, self.dropout_keep_prob, self.train_mode)
        return tf.matmul(state, self.decoding_w) + self.decoding_b

    def _get_rnn_cell(self) -> tf.contrib.rnn.RNNCell:
        return RNN_CELL_TYPES[self._rnn_cell_str](self.rnn_size)

    def _get_conditional_gru_cell(self) -> tf.contrib.rnn.GRUCell:
        return tf.contrib.rnn.GRUCell(self.rnn_size)

    def get_attention_object(self, encoder, train_mode: bool):
        if train_mode:
            return self._train_attention_objects.get(encoder)

        return self._runtime_attention_objects.get(encoder)

    def step(self,
             att_objects: List[BaseAttention],
             input_: tf.Tensor,
             prev_state: tf.Tensor,
             prev_attns: List[tf.Tensor]):

        with tf.variable_scope(self.step_scope):
            cell = self._get_rnn_cell()

            # Merge input and previous attentions into one vector of the
            # right size.
            if self._attention_on_input:
                x = linear([input_] + prev_attns, self.embedding_size)
            else:
                x = input_

            # Run the RNN.
            cell_output, state = cell(x, prev_state)

            # Run the attention mechanism.
            if self._rnn_cell_str == 'GRU':
                attns = [a.attention(cell_output, prev_state, x)
                         for a in att_objects]
            elif self._rnn_cell_str == 'LSTM':
                attns = [a.attention(cell_output, prev_state.c, x)
                         for a in att_objects]
            else:
                raise ValueError("Unknown RNN cell.")

            if self._conditional_gru and self._rnn_cell_str == "GRU":
                cell_cond = self._get_conditional_gru_cell()
                cond_input = tf.concat(attns, -1)
                cell_output, state = cell_cond(cond_input, state,
                                               scope="cond_gru_2_cell")

            with tf.name_scope("rnn_output_projection"):
                if attns:
                    output = linear([cell_output] + attns,
                                    cell.output_size,
                                    scope="AttnOutputProjection")
                else:
                    output = cell_output

            logits = self._logit_function(output)

        return logits, state, attns

    # pylint: disable=too-many-branches
    def _decoding_loop(
            self,
            go_symbols: tf.Tensor,
            train_inputs: tf.Tensor=None,
            train_mode: bool = False) -> Tuple[
                List[tf.Tensor], List[tf.Tensor], List[tf.Tensor]]:
        """Run the decoder RNN.

        Arguments:
            go_symbols: The tensor of start symbols of shape (1, batch_size)
            train_inputs: Training inputs to feed the decoder with. These are
                not used when `train_mode = False`
            train_mode: Boolean flag whether the decoder is running in
                train (with ground truth inputs) or runtime mode (with inputs
                decoded using the loop function)
            scope: Variable scope to use
        """
        att_objects = [self.get_attention_object(e, train_mode)
                       for e in self.encoders]
        att_objects = [a for a in att_objects if a is not None]

        if self._rnn_cell_str == 'GRU':
            state = self.initial_state
        elif self._rnn_cell_str == 'LSTM':
            state = tf.contrib.rnn.LSTMStateTuple(
                self.initial_state, self.initial_state)
        else:
            raise ValueError("Unknown RNN cell.")

        step_logits = None

        attns = [tf.zeros([self.batch_size, a.attn_size])
                 for a in att_objects]
        states = []  # type: List[tf.Tensor]
        logits = []  # type: List[tf.Tensor]

        mask = []  # type: List[tf.Tensor]
        finished = tf.zeros([self.batch_size], dtype=tf.bool)

        for i in range(self.max_output_len):
            if i > 0:
                self.step_scope.reuse_variables()

            # choose the input
            if step_logits is None:
                assert i == 0
                inp = go_symbols[0]
            elif train_mode:
                inp = train_inputs[i - 1]
            else:
                prev_word_index = tf.argmax(step_logits, 1)
                inp = self.embed_and_dropout(prev_word_index)

            # perform the RNN step
            step_logits, state, attns = self.step(
                att_objects, inp, state, attns)

            next_word_id = tf.argmax(step_logits, axis=1)
            has_just_finished = tf.equal(next_word_id, END_TOKEN_INDEX)
            finished = tf.logical_or(has_just_finished, finished)

            mask.append(tf.logical_not(finished))

            logits.append(step_logits)
            states.append(state)

        return logits, states, mask

    def _visualize_attention(self) -> None:
        """Create image summaries with attentions"""
        att_objects = self._runtime_attention_objects.values()

        for i, a in enumerate(att_objects):
            if not hasattr(a, "attentions_in_time"):
                continue

            alignments = tf.expand_dims(tf.transpose(
                tf.stack(a.attentions_in_time), perm=[1, 2, 0]), -1)

            tf.summary.image(
                "attention_{}".format(i), alignments,
                collections=["summary_val_plots"],
                max_outputs=256)

    def feed_dict(self, dataset: Dataset, train: bool = False) -> FeedDict:
        """Populate the feed dictionary for the decoder object

        Arguments:
            dataset: The dataset to use for the decoder.
            train: Boolean flag, telling whether this is a training run
        """
        sentences = cast(Iterable[List[str]],
                         dataset.get_series(self.data_id, allow_none=True))

        if sentences is None and train:
            raise ValueError("When training, you must feed "
                             "reference sentences")

        sentences_list = list(sentences) if sentences is not None else None

        fd = {}  # type: FeedDict
        fd[self.train_mode] = train

        go_symbol_idx = self.vocabulary.get_word_index(START_TOKEN)
        fd[self.go_symbols] = np.full([1, len(dataset)], go_symbol_idx,
                                      dtype=np.int32)

        if sentences is not None:
            # train_mode=False, since we don't want to <unk>ize target words!
            inputs, weights = self.vocabulary.sentences_to_tensor(
                sentences_list, self.max_output_len, train_mode=False,
                add_start_symbol=False, add_end_symbol=True)

            assert inputs.shape == (self.max_output_len, len(sentences_list))
            assert weights.shape == (self.max_output_len, len(sentences_list))

            fd[self.train_inputs] = inputs
            fd[self.train_padding] = weights

        return fd
