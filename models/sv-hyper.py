import os
import math
from datetime import datetime

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import GATConv
from torch_geometric.utils import dropout_adj

import utils
from geomloss import SamplesLoss
from geoopt import ManifoldParameter, PoincareBall

def hyperbolic_distance(z1, z2):
    return torch.acosh(1 + 2 * ((z1 - z2).norm(dim=1) ** 2) / (1 - (z1.norm(dim=1) ** 2)) / (1 - (z2.norm(dim=1) ** 2)))

def sliced_wasserstein_distance(z1, z2):
    loss = SamplesLoss(loss="sinkhorn", p=2, blur=0.05)
    return loss(z1, z2)

def node_node_loss(node_rep1, node_rep2):
    num_nodes = node_rep1.shape[0]
    pos_mask = torch.eye(num_nodes).cuda()
    return (hyperbolic_distance(node_rep1, node_rep2) * pos_mask).sum() / pos_mask.sum()

def seq_seq_loss(seq_rep1, seq_rep2):
    batch_size = seq_rep1.shape[0]
    pos_mask = torch.eye(batch_size).cuda()
    return (sliced_wasserstein_distance(seq_rep1, seq_rep2) * pos_mask).sum() / pos_mask.sum()

def node_seq_loss(node_rep, seq_rep, sequences):
    batch_size = seq_rep.shape[0]
    num_nodes = node_rep.shape[0]
    pos_mask = torch.zeros((batch_size, num_nodes + 1)).cuda()
    for row_idx, row in enumerate(sequences):
        pos_mask[row_idx][row] = 1.
    pos_mask = pos_mask[:, :-1]
    return (sliced_wasserstein_distance(seq_rep, node_rep) * pos_mask).sum() / pos_mask.sum() + hyperbolic_distance(seq_rep, node_rep).mean()

def weighted_ns_loss(node_rep, seq_rep, weights):
    return (sliced_wasserstein_distance(seq_rep, node_rep) * weights).sum() / weights.sum()

# def jsd(z1, z2, pos_mask):
#     neg_mask = 1 - pos_mask

#     sim_mat = torch.mm(z1, z2.t())
#     E_pos = math.log(2.) - F.softplus(-sim_mat)
#     E_neg = F.softplus(-sim_mat) + sim_mat - math.log(2.)
#     return (E_neg * neg_mask).sum() / neg_mask.sum() - (E_pos * pos_mask).sum() / pos_mask.sum()


# def nce(z1, z2, pos_mask):
#     sim_mat = torch.mm(z1, z2.t())
#     return nn.BCEWithLogitsLoss(reduction='none')(sim_mat, pos_mask).sum(1).mean()


# def ntx(z1, z2, pos_mask, tau=0.5, normalize=False):
#     if normalize:
#         z1 = F.normalize(z1)
#         z2 = F.normalize(z2)
#     sim_mat = torch.mm(z1, z2.t())
#     sim_mat = torch.exp(sim_mat / tau)
#     return -torch.log((sim_mat * pos_mask).sum(1) / sim_mat.sum(1) / pos_mask.sum(1)).mean()


# def node_node_loss(node_rep1, node_rep2, measure):
#     num_nodes = node_rep1.shape[0]

#     pos_mask = torch.eye(num_nodes).cuda()

#     if measure == 'jsd':
#         return jsd(node_rep1, node_rep2, pos_mask)
#     elif measure == 'nce':
#         return nce(node_rep1, node_rep2, pos_mask)
#     elif measure == 'ntx':
#         return ntx(node_rep1, node_rep2, pos_mask)


# def seq_seq_loss(seq_rep1, seq_rep2, measure):
#     batch_size = seq_rep1.shape[0]

#     pos_mask = torch.eye(batch_size).cuda()

#     if measure == 'jsd':
#         return jsd(seq_rep1, seq_rep2, pos_mask)
#     elif measure == 'nce':
#         return nce(seq_rep1, seq_rep2, pos_mask)
#     elif measure == 'ntx':
#         return ntx(seq_rep1, seq_rep2, pos_mask)


# def node_seq_loss(node_rep, seq_rep, sequences, measure):
#     batch_size = seq_rep.shape[0]
#     num_nodes = node_rep.shape[0]

#     pos_mask = torch.zeros((batch_size, num_nodes + 1)).cuda()
#     for row_idx, row in enumerate(sequences):
#         pos_mask[row_idx][row] = 1.
#     pos_mask = pos_mask[:, :-1]

#     if measure == 'jsd':
#         return jsd(seq_rep, node_rep, pos_mask)
#     elif measure == 'nce':
#         return nce(seq_rep, node_rep, pos_mask)
#     elif measure == 'ntx':
#         return ntx(seq_rep, node_rep, pos_mask)


# def weighted_ns_loss(node_rep, seq_rep, weights, measure):
#     if measure == 'jsd':
#         return jsd(seq_rep, node_rep, weights)
#     elif measure == 'nce':
#         return nce(seq_rep, node_rep, weights)
#     elif measure == 'ntx':
#         return ntx(seq_rep, node_rep, weights)


def random_mask(x, mask_token, mask_prob=0.2):
    mask_pos = torch.empty(
        x.size(),
        dtype=torch.float32,
        device=x.device).uniform_(0, 1) < mask_prob
    x = x.clone()
    x[mask_pos] = mask_token
    return x


class PositionalEncoding(nn.Module):
    def __init__(self, dim, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, dim)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, dim, 2).float() * (-math.log(10000.0) / dim))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class TransformerModel(nn.Module):
    def __init__(self, input_size, num_heads, hidden_size, num_layers, dropout=0.3):
        super(TransformerModel, self).__init__()
        self.pos_encoder = PositionalEncoding(input_size, dropout)
        encoder_layers = nn.TransformerEncoderLayer(input_size, num_heads, hidden_size, dropout)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layers, num_layers)

    def forward(self, src, src_mask, src_key_padding_mask):
        src = self.pos_encoder(src)
        output = self.transformer_encoder(src, src_mask, src_key_padding_mask)
        return output


class GraphEncoder(nn.Module):
    def __init__(self, input_size, output_size, encoder_layer, num_layers, activation):
        super(GraphEncoder, self).__init__()

        self.num_layers = num_layers
        self.activation = activation

        self.layers = [encoder_layer(input_size, output_size)]
        for _ in range(1, num_layers):
            self.layers.append(encoder_layer(output_size, output_size))
        self.layers = nn.ModuleList(self.layers)

    def forward(self, x, edge_index):
        for i in range(self.num_layers):
            x = self.activation(self.layers[i](x, edge_index))
        return x

# --- Hyperbolic Graph Encoder ---
class HyperbolicGraphEncoder(nn.Module):
    def __init__(self, input_size, output_size, encoder_layer, num_layers):
        super(HyperbolicGraphEncoder, self).__init__()
        self.num_layers = num_layers
        self.ball = PoincareBall()
        self.layers = nn.ModuleList()
        for _ in range(num_layers):
            self.layers.append(encoder_layer(input_size, output_size))
        self.node_embeddings = ManifoldParameter(self.ball.expmap0(torch.randn(input_size, output_size)), manifold=self.ball)

    def forward(self, x, edge_index):
        for layer in self.layers:
            x = self.ball.expmap0(layer(self.ball.logmap0(x), edge_index))
        return x

class SingleViewModel(nn.Module):
    def __init__(self, vocab_size, embed_size, hidden_size, edge_index, graph_encoder, seq_encoder, mode='p'):
        super(SingleViewModel, self).__init__()

        self.vocab_size = vocab_size
        self.edge_index = edge_index
        self.node_embedding = nn.Embedding(vocab_size, embed_size)
        self.padding = torch.zeros(1, hidden_size, requires_grad=False).cuda()
        self.graph_encoder = graph_encoder
        self.seq_encoder = seq_encoder
        self.mode = mode

    def encode_graph(self, drop_rate=0.):
        node_emb = self.node_embedding.weight
        edge_index = dropout_adj(self.edge_index, p=drop_rate)[0]
        node_enc = self.graph_encoder(node_emb, edge_index)
        return node_enc

    def encode_sequence(self, sequences, drop_rate=0.):
        if self.mode == 'p':
            lookup_table = torch.cat([self.node_embedding.weight, self.padding], 0)
        else:
            node_enc = self.encode_graph()
            lookup_table = torch.cat([node_enc, self.padding], 0)
        batch_size, max_seq_len = sequences.size()
        sequences = random_mask(sequences, self.vocab_size, drop_rate)
        src_key_padding_mask = (sequences == self.vocab_size)
        pool_mask = (1 - src_key_padding_mask.int()).transpose(0, 1).unsqueeze(-1)

        seq_emb = torch.index_select(
            lookup_table, 0, sequences.view(-1)).view(batch_size, max_seq_len, -1).transpose(0, 1)
        seq_enc = self.seq_encoder(seq_emb, None, src_key_padding_mask)
        seq_pooled = (seq_enc * pool_mask).sum(0) / pool_mask.sum(0)
        return seq_pooled

    def forward(self, sequences, drop_edge_rate=0., drop_road_rate=0.):
        node_rep = self.encode_graph(drop_edge_rate)
        seq_rep = self.encode_sequence(sequences, drop_road_rate)
        return node_rep, seq_rep

class UpdatedSingleViewModel(nn.Module):
    def __init__(self, vocab_size, embed_size, hidden_size, edge_index, seq_encoder, mode='p'):
        super(UpdatedSingleViewModel, self).__init__()
        self.vocab_size = vocab_size
        self.edge_index = edge_index
        self.node_embedding = nn.Embedding(vocab_size, embed_size)
        self.graph_encoder = HyperbolicGraphEncoder(embed_size, hidden_size, GATConv, 2)
        self.seq_encoder = seq_encoder
        self.mode = mode

    def encode_graph(self, drop_rate=0.0):
        node_emb = self.node_embedding.weight
        edge_index = dropout_adj(self.edge_index, p=drop_rate)[0]
        node_enc = self.graph_encoder(node_emb, edge_index)
        return node_enc
    
    def encode_sequence(self, sequences, drop_rate=0.):
        if self.mode == 'p':
            lookup_table = torch.cat([self.node_embedding.weight, self.padding], 0)
        else:
            node_enc = self.encode_graph()
            lookup_table = torch.cat([node_enc, self.padding], 0)
        batch_size, max_seq_len = sequences.size()
        sequences = random_mask(sequences, self.vocab_size, drop_rate)
        src_key_padding_mask = (sequences == self.vocab_size)
        pool_mask = (1 - src_key_padding_mask.int()).transpose(0, 1).unsqueeze(-1)

        seq_emb = torch.index_select(
            lookup_table, 0, sequences.view(-1)).view(batch_size, max_seq_len, -1).transpose(0, 1)
        seq_enc = self.seq_encoder(seq_emb, None, src_key_padding_mask)
        seq_pooled = (seq_enc * pool_mask).sum(0) / pool_mask.sum(0)
        return seq_pooled

    def forward(self, sequences, drop_edge_rate=0.0, drop_road_rate=0.0):
        node_rep = self.encode_graph(drop_edge_rate)
        seq_rep = self.encode_sequence(sequences, drop_road_rate)
        return node_rep, seq_rep

def train(data_path, save_path, data_files, num_nodes, edge_index, config):
    embed_size = config['embed_size']
    hidden_size = config['hidden_size']
    drop_rate = config['drop_rate']
    drop_edge_rate = config['drop_edge_rate']
    drop_road_rate = config['drop_road_rate']
    learning_rate = config['learning_rate']
    weight_decay = config['weight_decay']
    num_epochs = config['num_epochs']
    batch_size = config['batch_size']
    measure = config['loss_measure']
    is_weighted = config['weighted_loss']
    mode = config['mode']
    l_st = config['lambda_st']
    l_ss = l_tt = 0.5 * (1 - l_st)
    activation = {'relu': nn.ReLU(), 'prelu': nn.PReLU()}[config['activation']]

    graph_encoder = GraphEncoder(embed_size, hidden_size, GATConv, 2, activation)
    seq_encoder = TransformerModel(hidden_size, 4, hidden_size, 2, drop_rate)
    model = SingleViewModel(num_nodes, embed_size, hidden_size, edge_index, graph_encoder, seq_encoder, mode).cuda()
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)

    model_name = "_".join(['sv', str(config['lambda_st']), str(config['num_samples']), mode])
    checkpoints = [f for f in os.listdir(save_path) if f.startswith(model_name)]
    if not config['retrain'] and checkpoints:
        checkpoint_path = os.path.join(save_path, sorted(checkpoints)[-1])
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        current_epoch = checkpoint['epoch'] + 1
    else:
        model.apply(utils.weight_init)
        current_epoch = 1

    if current_epoch < num_epochs:
        data, w_rt = utils.train_data_loader(data_path, data_files, num_nodes, config)

        print("\n=== Training ===")
        for epoch in range(current_epoch, num_epochs + 1):
            for n, batch_index in enumerate(utils.next_batch_index(data.shape[0], batch_size)):
                data_batch = data[batch_index].cuda()
                w_batch = w_rt[batch_index].cuda() if w_rt is not None else 0
                model.train()
                optimizer.zero_grad()
                node_rep1, seq_rep1 = model(data_batch, drop_edge_rate, drop_road_rate)
                node_rep2, seq_rep2 = model(data_batch, drop_edge_rate, drop_road_rate)
                loss_ss = node_node_loss(node_rep1, node_rep2, measure)
                loss_tt = seq_seq_loss(seq_rep1, seq_rep2, measure)
                if is_weighted:
                    loss_st1 = weighted_ns_loss(node_rep1, seq_rep2, w_batch, measure)
                    loss_st2 = weighted_ns_loss(node_rep2, seq_rep1, w_batch, measure)
                else:
                    loss_st1 = node_seq_loss(node_rep1, seq_rep2, data_batch, measure)
                    loss_st2 = node_seq_loss(node_rep2, seq_rep1, data_batch, measure)
                loss_st = (loss_st1 + loss_st2) / 2
                loss = l_ss * loss_ss + l_tt * loss_tt + l_st * loss_st
                loss.backward()
                optimizer.step()
                if not (n + 1) % 200:
                    t = datetime.now().strftime('%m-%d %H:%M:%S')
                    print(f'{t} | (Train) | Epoch={epoch}, batch={n + 1} loss={loss.item():.4f}')

            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict()
            }, os.path.join(save_path, "_".join([model_name, f'{epoch}.pt'])))

    return model
