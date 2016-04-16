import sys
import numpy
import gzip
import argparse
from index_data import DataProcessor
from onto_attention import OntoAttentionLSTM
from keras.models import Model
from keras.layers import Activation, Dense, Dropout, Embedding, Input, LSTM, merge
from keras_extensions import HigherOrderEmbedding

class EntailmentModel(object):
  def __init__(self, embed_file, num_senses=2, num_hyps=5):
    self.dp = DataProcessor(word_syn_cutoff=num_senses, syn_path_cutoff=num_hyps)
    self.max_hyps_per_word = num_senses * num_hyps
    self.numpy_rng = numpy.random.RandomState(12345)
    self.word_rep = {}
    self.word_rep_max = -float("inf")
    self.word_rep_min = float("inf")
    for line in gzip.open(embed_file):
      ln_parts = line.strip().split()
      if len(ln_parts) == 2:
        continue
      word = ln_parts[0]
      vec = numpy.asarray([float(f) for f in ln_parts[1:]])
      vec_max, vec_min = vec.max(), vec.min()
      if vec_max > self.word_rep_max:
        self.word_rep_max = vec_max
      if vec_min < self.word_rep_min:
        self.word_rep_min = vec_min
      self.word_rep[word] = vec
    self.word_dim = len(vec)
    self.model = None

  def read_sentences(self, tagged_sentences, sentlenlimit=None):
    num_sentences = len(tagged_sentences)
    all_words = []
    all_pos_tags = []
    maxsentlen = 0
    for tagged_sentence in tagged_sentences:
      sent1_words = []
      sent1_pos_tags = []
      sent2_words = []
      sent2_pos_tags = []
      in_first_sent = True
      # Expects each token to be a "_" separated combination of word and POS tag.
      for word_tag in tagged_sentence.split(" "):
        if word_tag == "|||":
          in_first_sent = False
          continue
        else:
          word, tag = word_tag.split("_")
        word = word.lower()
        if in_first_sent:
          sent1_words.append(word)
          sent1_pos_tags.append(tag)
        else:
          sent2_words.append(word)
          sent2_pos_tags.append(tag)
      if len(sent1_words) > maxsentlen:
        maxsentlen = len(sent1_words)
      if len(sent2_words) > maxsentlen:
        maxsentlen = len(sent2_words)
      all_words.append((sent1_words, sent2_words))
      all_pos_tags.append((sent1_pos_tags, sent2_pos_tags))
    if not sentlenlimit:
      sentlenlimit = maxsentlen
    C1_ind = numpy.zeros((num_sentences, sentlenlimit, self.max_hyps_per_word), dtype='int32')
    S1_ind = numpy.zeros((num_sentences, sentlenlimit), dtype='int32')
    C2_ind = numpy.zeros((num_sentences, sentlenlimit, self.max_hyps_per_word), dtype='int32')
    S2_ind = numpy.zeros((num_sentences, sentlenlimit), dtype='int32')
    S1 = numpy.zeros((num_sentences, sentlenlimit, self.word_dim))
    S2 = numpy.zeros((num_sentences, sentlenlimit, self.word_dim))
    for i, ((sent1_words, sent2_words), (sent1_pos_tags, sent2_pos_tags)) in enumerate(zip(all_words, all_pos_tags)):
      for word in sent1_words + sent2_words:
        if word not in self.word_rep:
          rand_rep = self.numpy_rng.uniform(low=self.word_rep_min, high=self.word_rep_max, size=(self.word_dim))
          self.word_rep[word] = rand_rep
      # Sentence 1 processing
      sent1len = len(sent1_words)
      sent1_word_inds, sent1_syn_inds = self.dp.index_sentence(sent1_words, sent1_pos_tags)
      S1_ind[i][-sent1len:] = sent1_word_inds
      for j in range(sent1len):
        S1[i][-sent1len+j] = self.word_rep[sent1_words[j]]
        syn_ind = sent1_syn_inds[j]
        C1_ind[i][-sent1len+j][-len(syn_ind):] = syn_ind
      # Sentence 2 processing
      sent2len = len(sent2_words)
      sent2_word_inds, sent2_syn_inds = self.dp.index_sentence(sent2_words, sent2_pos_tags)
      S2_ind[i][-sent2len:] = sent2_word_inds
      for j in range(sent2len):
        S2[i][-sent2len+j] = self.word_rep[sent2_words[j]]
        syn_ind = sent2_syn_inds[j]
        C2_ind[i][-sent2len+j][-len(syn_ind):] = syn_ind
    return (S1, S2), (S1_ind, S2_ind), (C1_ind, C2_ind)

  def train(self, S1_ind, S2_ind, C1_ind, C2_ind, label_ind, num_label_types, ontoLSTM=False, use_attention=False, num_epochs=20, embedding=None, tune_embedding=True):
    word_dim = 50
    assert S1_ind.shape == S2_ind.shape
    assert C1_ind.shape == C2_ind.shape
    num_words = len(self.dp.word_index)
    num_syns = len(self.dp.synset_index)
    length = C1_ind.shape[1]
    label_onehot = numpy.zeros((len(label_ind), num_label_types))
    for i, ind in enumerate(label_ind):
      label_onehot[i][ind] = 1.0
    if ontoLSTM:
      print >>sys.stderr, "Using OntoLSTM"
      if tune_embedding:
        sent1 = Input(name='sent1', shape=C1_ind.shape[1:], dtype='int32')
        sent2 = Input(name='sent2', shape=C2_ind.shape[1:], dtype='int32')
        model_inputs = [sent1, sent2]
        if embedding is None:
          embedding_layer = HigherOrderEmbedding(input_dim=num_syns, output_dim=word_dim, name='embedding')
        else:
          embedding_layer = HigherOrderEmbedding(input_dim=num_syns, output_dim=word_dim, weights=[embedding], name='embedding')
        sent1_embedding = embedding_layer(sent1)
        sent2_embedding = embedding_layer(sent2)
      else:
        assert embedding is not None, "If you wish to fix the embedding (tune_embedding == False), initialize it (embedding should not be None)"
        embed_dim = embedding.shape[1]
        sent1_embedding = Input(name='sent1_embedding', shape=(C1_ind.shape[1:], C1_ind.shape[2], embed_dim))
        sent1_embedding = Input(name='sent2_embedding', shape=(C2_ind.shape[1:], C2_ind.shape[2], embed_dim))
        model_inputs = [sent1_embedding, sent2_embedding]
      sent1_dropout = Dropout(0.5)(sent1_embedding)
      sent2_dropout = Dropout(0.5)(sent2_embedding)
      lstm = OntoAttentionLSTM(input_dim=word_dim, output_dim=word_dim/2, input_length=length, num_hyps=self.max_hyps_per_word, use_attention=use_attention, name='sent_lstm')
      sent1_lstm_output = lstm(sent1_dropout)
      sent2_lstm_output = lstm(sent2_dropout)
      merged_sent_rep = merge([sent1_lstm_output, sent2_lstm_output], mode='concat')
      softmax = Dense(output_dim=num_label_types, activation='softmax')
      label_probs = softmax(merged_sent_rep)
      model = Model(input=model_inputs, output=label_probs)
      print >>sys.stderr, model.summary()
      model.compile(optimizer='adam', loss='categorical_crossentropy', metrics=['accuracy'])
      data_size = C1_ind.shape[0]
      train_size = int(data_size * 0.9)
      model.fit([C1_ind[:train_size], C2_ind[:train_size]], label_onehot[:train_size], nb_epoch=20, validation_data=([C1_ind[train_size:], C2_ind[train_size:]], label_onehot[train_size:]))
      self.model = model
    else:
      print >>sys.stderr, "Using traditional LSTM"
      sent1 = Input(name='sent1', shape=S1_ind.shape[1:], dtype='int32')
      sent2 = Input(name='sent2', shape=S2_ind.shape[1:], dtype='int32')
      embedding_layer = Embedding(input_dim=num_words, output_dim=word_dim, name='embedding')
      sent1_embedding = embedding_layer(sent1)
      sent2_embedding = embedding_layer(sent2)
      sent1_dropout = Dropout(0.5)(sent1_embedding)
      sent2_dropout = Dropout(0.5)(sent2_embedding)
      lstm = LSTM(input_dim=word_dim, output_dim=word_dim/2, input_length=length, name='sent_lstm')
      sent1_lstm_out = lstm(sent1_dropout)
      sent2_lstm_out = lstm(sent2_dropout)
      merged_sent_rep = merge([sent1_lstm_out, sent2_lstm_out], mode='concat')
      softmax = Dense(output_dim=num_label_types, activation='softmax')
      label_probs = softmax(merged_sent_rep)
      model = Model(input=[sent1, sent2], output=label_probs)
      print >>sys.stderr, model.summary()
      model.compile(optimizer='adam', loss='categorical_crossentropy', metrics=['accuracy'])
      data_size = S1_ind.shape[0]
      train_size = int(data_size * 0.9)
      model.fit([S1_ind[:train_size], S2_ind[:train_size]], label_onehot[:train_size], nb_epoch=20, validation_data=([S1_ind[train_size:], S2_ind[train_size:]], label_onehot[train_size:]))
      self.model = model

  def get_attention(self, C_ind, embedding=None):
    if not self.model:
      raise RuntimeError, "Model not trained!"
    embedding_given = False if embedding is None else True
    model_embedding = None
    model_lstm = None
    for layer in self.model.layers:
      if layer.name == "embedding":
        model_embedding = layer
      if layer.name == "sent_lstm":
        model_lstm = layer
    if not (model_embedding or embedding_given) or not model_lstm:
      raise RuntimeError, "Did not find the layers expected"
    lstm_weights = model_lstm.get_weights()
    import pickle
    pkl_file = open("lstm_weights.pkl", "wb")
    pickle.dump(lstm_weights, pkl_file)
    pkl_file.close()
    if not embedding_given:
      sent = Input(shape=C_ind.shape[1:], dtype='int32')
      embedding_weights = model_embedding.get_weights()
      embed_in_dim, embed_out_dim = embedding_weights[0].shape
      att_embedding = HigherOrderEmbedding(input_dim=embed_in_dim, output_dim=embed_out_dim, weights=embedding_weights)
      sent_embedding = att_embedding(sent)
      att_input = sent
    else:
      _, embed_out_dim = embedding.shape
      sent_embedding = Input(shape=(C_ind.shape[1], C_ind.shape[2], embed_out_dim))
      att_input = sent_embedding
    onto_lstm = OntoAttentionLSTM(input_dim=embed_out_dim, output_dim=embed_out_dim/2, input_length=model_lstm.input_length, num_hyps=self.max_hyps_per_word, use_attention=True, return_attention=True, weights=lstm_weights)
    att_output = onto_lstm(sent_embedding)
    att_model = Model(input=att_input, output=att_output)
    att_model.compile(optimizer='adam', loss='mse') # optimizer and loss are not needed since we are not going to train this model.
    C_att = att_model.predict(C_ind) if not embedding_given else att_model.predict(embedding[C_ind])
    print >>sys.stderr, "Got attention values. Input, output shapes:", C_ind.shape, C_att.shape
    return C_att

if __name__ == "__main__":
  argparser = argparse.ArgumentParser(description="Train entailment model using ontoLSTM or traditional LSTM")
  argparser.add_argument('repfile', metavar='REP-FILE', type=str, help="Gzipped word embedding file")
  argparser.add_argument('train_file', metavar='TRAIN-FILE', type=str, help="TSV file with label, premise, hypothesis in three columns")
  argparser.add_argument('--use_onto_lstm', help="Use ontoLSTM. If this flag is not set, will use traditional LSTM", action='store_true')
  argparser.add_argument('--num_senses', type=int, help="Number of senses per word if using OntoLSTM (default 2)", default=2)
  argparser.add_argument('--num_hyps', type=int, help="Number of hypernyms per sense if using OntoLSTM (default 5)", default=5)
  argparser.add_argument('--use_attention', help="Use attention in ontoLSTM. If this flag is not set, will use average concept representations", action='store_true')
  argparser.add_argument('--attention_output', type=str, help="Print attention values of the validation data in the given file")
  argparser.add_argument('--synset_embedding', type=str, help="File with synset vectors")
  argparser.add_argument('--fix_embedding', help="File with synset vectors", action='store_true')
  argparser.add_argument('--num_epochs', type=int, help="Number of epochs (default 20)", default=20)
  args = argparser.parse_args()
  use_synset_embedding = False
  vec_max = -float("inf")
  vec_min = float("inf")
  if args.synset_embedding:
    synset_embedding = {}
    for line in gzip.open(args.synset_embedding):
      ln_parts = line.strip().split()
      if len(ln_parts) == 2:
        continue
      word = ln_parts[0]
      vec = numpy.asarray([float(f) for f in ln_parts[1:]])
      vec_max = max(max(vec), vec_max)
      vec_min = min(min(vec), vec_min)
      synset_embedding[word] = vec
    vec_dim = len(vec)
    use_synset_embedding = True
  em = EntailmentModel(args.repfile, num_senses=args.num_senses, num_hyps=args.num_hyps)
  tagged_sentences = []
  label_map = {}
  label_ind = []
  for line in open(args.train_file):
    lnstrp = line.strip()
    label, tagged_sentence = lnstrp.split("\t")
    if label not in label_map:
      label_map[label] = len(label_map)
    label_ind.append(label_map[label])
    tagged_sentences.append(tagged_sentence)
  _, (S1_ind, S2_ind), (C1_ind, C2_ind) = em.read_sentences(tagged_sentences)
  if use_synset_embedding:
    ind_synset_embedding = em.numpy_rng.uniform(low=vec_min, high=vec_max, size=(len(em.dp.synset_index), vec_dim))
    for syn in em.dp.synset_index:
      if syn in synset_embedding:
        ind_synset_embedding[em.dp.synset_index[syn]] = synset_embedding[syn]
    print >>sys.stderr, "Using pretrained synset embeddings"
    em.train(S1_ind, S2_ind, C1_ind, C2_ind, label_ind, len(label_map), ontoLSTM=args.use_onto_lstm, use_attention=args.use_attention, num_epochs=args.num_epochs, embedding=ind_synset_embedding)
  else: 
    print >>sys.stderr, "Will learn synset embeddings"
    em.train(S1_ind, S2_ind, C1_ind, C2_ind, label_ind, len(label_map), ontoLSTM=args.use_onto_lstm, use_attention=args.use_attention, num_epochs=args.num_epochs)

  if args.attention_output is not None:
    rev_synset_ind = {ind: syn for (syn, ind) in em.dp.synset_index.items()}
    sample_size = int(C1_ind.shape[0] * 0.1)
    C_ind = numpy.concatenate([C1_ind[-sample_size:], C2_ind[-sample_size:]])
    C_att = em.get_attention(C_ind, ind_synset_embedding) if args.fix_embedding else em.get_attention(C_ind) 
    C1_att, C2_att = numpy.split(C_att, 2)
    # Concatenate sentence 1 and 2 in each data point
    C_sj_ind = numpy.concatenate([C1_ind[-sample_size:], C2_ind[-sample_size:]], axis=1)
    C_sj_att = numpy.concatenate([C1_att, C2_att], axis=1)
    outfile = open(args.attention_output, "w")
    for i, (sent, sent_c_inds, sent_c_atts) in enumerate(zip(tagged_sentences[-sample_size:], C_sj_ind, C_sj_att)):
      print >>outfile, "SENT %d: %s"%(i, sent)
      for word_c_inds, word_c_atts in zip(sent_c_inds, sent_c_atts):
        if sum(word_c_inds) == 0:
          continue
        for c_ind, c_att in zip(word_c_inds, word_c_atts):
          if c_ind == 0:
            continue
          print >>outfile, rev_synset_ind[c_ind], c_att 
        print >>outfile
      print >>outfile
