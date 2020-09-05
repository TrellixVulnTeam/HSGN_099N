from transformers import AdamW, BertConfig
import os
import json
import pickle
import warnings
import re

from os import listdir
from os.path import isfile, join
from dgl.data.utils import load_graphs

from tqdm import tqdm, trange

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import *

import dgl.function as fn
from dgl.nn.pytorch import edge_softmax, GATConv

import dgl
from dgl.nn.pytorch.conv import GATConv, RelGraphConv

warnings.filterwarnings(action='once')

os.environ['DGLBACKEND'] = 'pytorch'

import random
random_seed = 2020
# Set the seed value all over the place to make this reproducible.
random.seed(random_seed)
np.random.seed(random_seed)
torch.manual_seed(random_seed)
torch.cuda.manual_seed_all(random_seed)

#pretrained_weights = 'bert-base-cased'
pretrained_weights = 'bert-large-cased-whole-word-masking'
device = 'cuda'

weights = torch.tensor([1., 30.9, 31.], device=device)

# %%
loss_fn_ans_type = nn.CrossEntropyLoss(weights)

# %%
def get_sent_node_from_srl_node(graph, srl_node, list_srl_nodes):
    _, out_srl = graph.out_edges(srl_node)
    list_sent = list(set(out_srl.numpy()) - set(list_srl_nodes))
    # there is only one element by construction of the graph
    return list_sent[0]


# %%
class LabelSmoothingLoss(nn.Module):
    def __init__(self, classes=2, smoothing=0.1, dim=-1):
        super(LabelSmoothingLoss, self).__init__()
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.cls = classes
        self.dim = dim

    def forward(self, pred, target):
        pred = pred.log_softmax(dim=self.dim)
        with torch.no_grad():
            # true_dist = pred.data.clone()
            true_dist = torch.zeros_like(pred)
            true_dist.fill_(self.smoothing / (self.cls - 1))
            true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        return torch.mean(torch.sum(-true_dist * pred, dim=self.dim))


# %%
loss_fn = LabelSmoothingLoss()


class GAT(nn.Module):
    def __init__(self,
                 num_layers,
                 in_dim,
                 num_hidden,
                 num_classes,
                 num_heads = 1,
                 feat_drop = 0.1,
                 attn_drop = 0.1,
                 negative_slope = None,
                 residual = True,
                 activation = None):
        super(GAT, self).__init__()
        self.num_layers = num_layers
        self.gat_layers = nn.ModuleList()
        self.activation = activation
        # input projection (no residual)
        self.gat_layers.append(GATConv(in_dim, num_hidden, num_heads,
                                             feat_drop = feat_drop,
                                             attn_drop =attn_drop, 
                                             residual= residual,
                                             activation=activation))
        # hidden layers
        for l in range(1, num_layers):
            # due to multi-head, the in_dim = num_hidden * num_heads
            self.gat_layers.append(GATConv( num_hidden * num_heads, num_hidden, num_heads,
                                             feat_drop = feat_drop,
                                             attn_drop =attn_drop, 
                                             residual= residual,
                                             activation=activation))
        # output projection
        self.gat_layers.append(GATConv( num_hidden * num_heads, num_hidden, num_heads,
                                             feat_drop = feat_drop,
                                             attn_drop =attn_drop, 
                                             residual= residual,
                                             activation=activation))

    def forward(self, g, h):
        for l in range(self.num_layers):
            h = self.gat_layers[l](g, h).flatten(1)
        # output projection
        logits = self.gat_layers[-1](g, h).mean(1)
        return logits


# %%
class HeteroRGCNLayer(nn.Module):
    def __init__(self, in_size, out_size, feat_drop = 0., attn_drop = 0., residual = False):
        super(HeteroRGCNLayer, self).__init__()
        self.in_size = in_size
        # W_r for each edge type
        self.tok_trans = nn.Linear(in_size, out_size)
        self.tok_att = nn.Linear(2 * in_size, out_size)

        self.rel_trans = nn.Linear(2 * in_size, out_size)

        self.node_trans = nn.Linear(in_size, out_size)
        self.node_att = nn.Linear(2 * in_size, out_size)

        self.common_space_trans = nn.Linear(in_size, out_size)
        
        self.gru_node2tok = nn.GRU(in_size, out_size)
        
        self.feat_drop = nn.Dropout(feat_drop)
        self.attn_drop = nn.Dropout(attn_drop)
        
        self.at_trans = nn.Linear(in_size, out_size)
        self.at_att = nn.Linear(2 * in_size, out_size)

        # self.common_space = nn.Linear(in_size, out_size)
        self.residual = residual

        self.reset_parameters()


    def message_func_rel(self, bert_token_emb, edges):
        '''
        m_ij = R_ji * W * h_j
        '''
        # relation emb
        rel_span_idx = edges.data['span_idx']  # idx at context level
        rel_emb = []
        for i, (x, y) in enumerate(rel_span_idx):
            # rel type = {1, -1}
            rel_emb.append(edges.data['rel_type'][i] * torch.mean(bert_token_emb[x:y], dim=0))
        rel_emb = torch.stack(rel_emb, dim=0)
        assert not torch.isnan(rel_emb).any()
#         return {'rel': rel_emb, 'srl': edges.src['h']}
        src = edges.src['h']
        m = self.common_space_trans(self.rel_trans(torch.cat((src, rel_emb), dim=1)))
        # m: [num srl x 768]
        dst = self.node_trans(edges.dst['h'])
        cat_uv = torch.cat([m,
                            dst],
                           dim=1)
        e = F.leaky_relu(self.node_att(cat_uv))
        return {'m': m, 'e': e}

    def reduce_func(self, nodes):
        '''
        h_srl = sum_j(h_j) + h_srl # w/o transformation for h_srl for now
        
        '''       
        alpha = self.attn_drop(F.softmax(nodes.mailbox['e'], dim=1))
        h = torch.sum(alpha * nodes.mailbox['m'], dim=1)
        return {'h': h}
    
    def message_func_2tok(self, edges):
        '''
        e_ij = LeakyReLU(W * (Wh_j || Wh_i))
        '''
        src = edges.src['h'].view(1,-1,self.in_size)
        tok = edges.dst['h'].view(1,-1,self.in_size) # the token node
        m = self.gru_node2tok(src, tok)[0].squeeze(0)
        return {'m': m}
    
    def reduce_func_srl2tok(self, nodes):
        h = torch.sum(nodes.mailbox['m'], dim=1)
        return {'h_srl': h}
    
    def reduce_func_srl2tok(self, nodes):
        h = torch.sum(nodes.mailbox['m'], dim=1)
        return {'h_ent': h}
    
    def message_func_regular_node(self, edges):
        '''
        e_ij = alpha_ij * W * h_j
        alpha_ij = LeakyReLU(W * (Wh_j || Wh_i))
        '''
        src = edges.src['h']
        updt_dst = self.node_trans(edges.dst['h'])
        updt_src = self.node_trans(src)
        cat_uv = torch.cat([updt_src,
                            updt_dst],
                           dim=1)
        e = F.leaky_relu(self.node_att(cat_uv))
        return {'m': updt_src, 'e': e,}
    
    def message_func_AT_node(self, edges):
        '''
        e_ij = alpha_ij * W * h_j
        alpha_ij = LeakyReLU(W * (Wh_j || Wh_i))
        '''
        src = edges.src['h']
        updt_dst = self.at_trans(edges.dst['h'])
        updt_src = self.at_trans(src)
        cat_uv = torch.cat([updt_src,
                            updt_dst],
                           dim=1)
        e = F.leaky_relu(self.at_att(cat_uv))
        return {'m': updt_src, 'e': e,}    
    
    def forward(self, G, feat_dict, bert_token_emb):
        # The input is a dictionary of node features for each type
        funcs = {}
#         G.nodes['AT'].data['h'] = self.feat_drop(feat_dict['AT']) # AT is never a src
#         if self.residual:
#             G.nodes['AT'].data['resid'] = feat_dict['AT']
                
        for srctype, etype, dsttype in G.canonical_etypes:
            G.nodes[srctype].data['h'] = self.feat_drop(feat_dict[srctype])
            
            if self.residual:
                G.nodes[srctype].data['resid'] = feat_dict[srctype]
            if "2tok" in etype:     
                pass
            elif "srl2srl" == etype:
                pass
            elif "ent2ent_rel" == etype:
                funcs[etype] = ((lambda e: self.message_func_rel(bert_token_emb, e)) , self.reduce_func)
            elif "sent2at" == etype:
                funcs[etype] = (self.message_func_AT_node, self.reduce_func)
            else:
                funcs[etype] = (self.message_func_regular_node, self.reduce_func)
        G.multi_update_all(funcs, 'sum')
        ## update tokens
        if 'srl' in G.ntypes:
            G['srl2tok'].update_all(self.message_func_2tok, fn.sum('m', 'h_srl'))
        if 'ent' in G.ntypes:
            G['ent2tok'].update_all(self.message_func_2tok, fn.sum('m', 'h_ent'))
        #batched all tokens since we want to put into the GRU (srl, ent, hidden=tok) so batch size = 512
        h_tok = G.nodes['tok'].data['h'].view(1, -1, self.in_size)
        gru_input = h_tok
        initial_hidden = h_tok
        if 'h_srl' in G.nodes['tok'].data:
            h_srl = G.nodes['tok'].data.pop('h_srl').view(1, -1, self.in_size)
            initial_hidden = h_srl
        if 'h_ent' in G.nodes['tok'].data:
            # there can be an instance without entities (not common anyway)
            h_ent = G.nodes['tok'].data.pop('h_ent').view(1, -1, self.in_size)
            gru_input = torch.cat((h_ent, h_tok), dim=0)           
        G.nodes['tok'].data['h'] = self.gru_node2tok(gru_input, initial_hidden)[0][-1]
        
        out = None
        if self.residual:
            out = {ntype : (G.nodes[ntype].data['h'] + G.nodes[ntype].data['resid']) for ntype in G.ntypes}
            # remove resid from the memory since it's not needed
            for ntype in G.ntypes:
                G.nodes[ntype].data.pop('resid')                 
        else:
            out = {ntype : G.nodes[ntype].data.pop('h') for ntype in G.ntypes}

        # return the updated node feature dictionary
        self.clean_memory(G)
        return out
   
    def reset_parameters(self):
        """Reinitialize learnable parameters."""
        gain = nn.init.calculate_gain('relu')
        nn.init.xavier_normal_(self.node_trans.weight, gain=gain)
        nn.init.xavier_normal_(self.node_att.weight, gain=gain)
        for param in self.gru_node2tok.parameters():
            if len(param.shape) >= 2:
                nn.init.orthogonal_(param.data)
            else:
                nn.init.normal_(param.data)
        
    def clean_memory(self, graph):
        # remove garbage from the graph computation
        node_tensors = ['h']
        for ntype in graph.ntypes:
            for key in node_tensors:
                if key in graph.nodes[ntype].data.keys():
                    del graph.nodes[ntype].data[key]
        
        edges_tensors = ['e', 'm']
        for (_, etype, _) in graph.canonical_etypes:
            for key in edges_tensors:
                if key in graph.edges[etype].data.keys():
                    del graph.edges[etype].data[key]


# %%
class HeteroRGCN(nn.Module):
    def __init__(self, in_size, hidden_size, out_size, feat_drop, attn_drop, residual):
        super(HeteroRGCN, self).__init__()
        self.in_size = in_size
        self.layer1 = HeteroRGCNLayer(in_size, hidden_size, feat_drop, attn_drop, residual)
        self.layer2 = HeteroRGCNLayer(hidden_size, out_size, feat_drop, attn_drop, residual)
        self.gru_layer_lvl = nn.GRU(in_size, out_size)
        
        self.init_params()
        
    def forward(self, G, emb, bert_token_emb):
        h_tok0 = emb['tok'].view(1,-1,self.in_size)
        
        h_dict = self.layer1(G, emb, bert_token_emb)
        h_tok1 = h_dict['tok'].view(1,-1,self.in_size)
        h_dict = {k : F.leaky_relu(h) for k, h in h_dict.items()}
        
        h_dict = self.layer2(G, h_dict, bert_token_emb)
        h_tok2 = h_dict['tok'].view(1,-1,self.in_size)
        
        #tok1, tok2 form a sequence and the initial hidden emb is tok0
        gru_input = torch.cat((h_tok1, h_tok2), dim=0)
        tok_emb = self.gru_layer_lvl(gru_input, h_tok0)[0][-1].view(-1, self.in_size)
        h_dict['tok'] = tok_emb
        return h_dict
    
    def init_params(self):
        for param in self.gru_layer_lvl.parameters():
            if len(param.shape) >= 2:
                nn.init.orthogonal_(param.data)
            else:
                nn.init.normal_(param.data)


# %%
bert_dim = 768 # default
if 'large' in pretrained_weights:
    bert_dim = 1024
dict_params = {'in_feats': bert_dim, 'out_feats': bert_dim, 'feat_drop': 0.1, 'attn_drop': 0.1, 'residual': True, 'hidden_size_classifier': 768,
               'weight_sent_loss': 1, 'weight_srl_loss': 1, 'weight_ent_loss': 1,
               'weight_span_loss': 2, 'weight_ans_type_loss': 1, 
               'gat_layers': 2}
class HGNModel(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.bert = BertModel(config)
        # graph
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

#         self.gat = GAT(dict_params['gat_layers'], dict_params['in_feats'], dict_params['in_feats'], dict_params['out_feats'],
#                            2, feat_drop = dict_params['feat_drop'],
#                            attn_drop = dict_params['attn_drop'])
        # Initial Node Embedding
        self.bigru = nn.GRU(dict_params['in_feats'], dict_params['in_feats'], 
                            bidirectional=True)
        self.gru_aggregation = nn.Linear(2*dict_params['in_feats'], dict_params['in_feats'])
        # Graph Neural Network
        self.rgcn = HeteroRGCN(dict_params['in_feats'], dict_params['in_feats'],
                               dict_params['in_feats'], dict_params['feat_drop'], dict_params['attn_drop'], 
                               dict_params['residual'])
        ## node classification
        ### ent node
        self.dropout_ent = nn.Dropout(config.hidden_dropout_prob)
        self.ent_classifier = nn.Sequential(nn.Linear(2*dict_params['out_feats'],
                                                      dict_params['hidden_size_classifier']),
                                            nn.ReLU(),
                                            nn.Linear(dict_params['hidden_size_classifier'],
                                                      2))
        ### srl node
        self.dropout_srl = nn.Dropout(config.hidden_dropout_prob)
        self.srl_classifier = nn.Sequential(nn.Linear(2*dict_params['out_feats'],
                                                      dict_params['hidden_size_classifier']),
                                            nn.ReLU(),
                                            nn.Linear(dict_params['hidden_size_classifier'],
                                                      2))
        ### sent node
        self.dropout_sent = nn.Dropout(config.hidden_dropout_prob)
        self.sent_classifier = nn.Sequential(nn.Linear(2*dict_params['out_feats'],
                                                       dict_params['hidden_size_classifier']),
                                            nn.ReLU(),
                                            nn.Linear(dict_params['hidden_size_classifier'],
                                                      2))
        
        # span prediction
        self.num_labels = config.num_labels
        self.qa_outputs = nn.Linear(config.hidden_size, config.num_labels)

        # ans type prediction
#         self.dropout_ans_type = nn.Dropout(config.hidden_dropout_prob)
#         self.ans_type_classifier = nn.Sequential(nn.Linear(2*dict_params['out_feats'],
#                                                            int(dict_params['hidden_size_classifier'])),
#                                                  nn.ReLU(),
#                                                  nn.Linear(int(dict_params['hidden_size_classifier']),
#                                                            3))
        # init weights
        self.init_weights()
        # params
        self.weight_sent_loss = dict_params['weight_sent_loss']
        self.weight_srl_loss = dict_params['weight_srl_loss']
        self.weight_ent_loss = dict_params['weight_ent_loss']
        self.weight_span_loss = dict_params['weight_span_loss']
        self.weight_ans_type_loss = dict_params['weight_ans_type_loss']
    
    def forward(
        self,
        graph=None,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
        train=True
    ):
        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds
        )
        sequence_output = outputs[0]
        assert not torch.isnan(sequence_output).any()
        # Graph forward & node classification
        graph_out, graph_emb = self.graph_forward(graph, sequence_output, train)
        sequence_output = graph_emb['tok'].unsqueeze(0)
        # span prediction
        span_loss = None
        start_logits = None
        end_logits = None
        span_loss, start_logits, end_logits = self.span_prediction(sequence_output, start_positions, end_positions)
        assert not torch.isnan(start_logits).any()
        assert not torch.isnan(end_logits).any()

        return { 
                'sent': graph_out['sent'], 
                'ent': graph_out['ent'],
                'srl': graph_out['srl'],
                'span': {'start_logits': start_logits, 'end_logits': end_logits}}  
    
    def graph_forward(self, graph, bert_context_emb, train):
        # create graph initial embedding #
        graph_emb = self.graph_initial_embedding(graph, bert_context_emb)
        for (k,v) in graph_emb.items():
            assert not torch.isnan(v).any()
        # graph_emb shape [num_nodes, in_feats] 
        #
        if train:   
            sample_sent_nodes = self.sample_sent_nodes(graph)
            sample_srl_nodes = self.sample_srl_nodes(graph)
            sample_ent_nodes = self.sample_ent_nodes(graph)
        initial_graph_emb = graph_emb # for skip-connection
        
        # update graph embedding #
        graph_emb = self.rgcn(graph, graph_emb, bert_context_emb[0])
        
        # graph_emb shape [num_nodes, num_heads, in_feats] num_heads = 1
#         graph_emb = graph_emb.view(-1, dict_params['out_feats'])
#         # graph_emb shape [num_nodes, in_feats]

        # classify nodes #
        sent_labels = None
        ent_labels = None
        if train:
            # add skip-connection
            logits_sent = self.sent_classifier(torch.cat((graph_emb['sent'][sample_sent_nodes],
                                                          initial_graph_emb['sent'][sample_sent_nodes]), dim=1))
            assert not torch.isnan(logits_sent).any() 
            
            # contains the indexes of the srl nodes
            if len(sample_srl_nodes) == 0:
                logits_srl = None
                srl_labels = None
            else:
                logits_srl = self.srl_classifier(torch.cat((graph_emb['srl'][sample_srl_nodes],
                                                            initial_graph_emb['srl'][sample_srl_nodes]), dim=1))
                # shape [num_ent_nodes, 2] 
                assert not torch.isnan(logits_srl).any()
                srl_labels = graph.nodes['srl'].data['labels'][sample_srl_nodes].to(device)
                # shape [num_sampled_srl_nodes, 1]
            
            # contains the indexes of the ent nodes
            if len(sample_ent_nodes) == 0:
                logits_ent = None
                ent_labels = None
            else:
                logits_ent = self.ent_classifier(torch.cat((graph_emb['ent'][sample_ent_nodes],
                                                            initial_graph_emb['ent'][sample_ent_nodes]), dim=1))
                # shape [num_ent_nodes, 2] 
                assert not torch.isnan(logits_ent).any()
                ent_labels = graph.nodes['ent'].data['labels'][sample_ent_nodes].to(device)
                # shape [num_sampled_ent_nodes, 1]    
            sent_labels = graph.nodes['sent'].data['labels'][sample_sent_nodes].to(device)
            # shape [num_sampled_sent_nodes, 1]
            
            
        else:
            # add skip-connection
            logits_sent = self.sent_classifier(torch.cat((graph_emb['sent'],
                                                          initial_graph_emb['sent']), dim=1))
            assert not torch.isnan(logits_sent).any()
            if 'srl' in graph.ntypes:
                logits_srl = self.srl_classifier(torch.cat((graph_emb['srl'],
                                                            initial_graph_emb['srl']), dim=1))
                assert not torch.isnan(logits_srl).any()
            else:
                logits_srl = None
            # shape [num_ent_nodes, 2] 
            logits_ent = None
            ent_labels = None
            if 'ent' in graph.ntypes:
                logits_ent = self.ent_classifier(torch.cat((graph_emb['ent'],
                                                            initial_graph_emb['ent']), dim=1))
                # shape [num_ent_nodes, 2]
                assert not torch.isnan(logits_ent).any()
                # shape [num_srl_nodes, 1]

        # sent loss
        probs_sent = F.softmax(logits_sent, dim=1).cpu()
        # shape [num_sent_nodes, 2]
        
        # srl loss
        loss_srl = None # not all ans are inside an srl arg
        probs_ent = torch.tensor([], device=device)
        if logits_srl is None:
            loss_srl = None
            probs_srl = None
        else:
            probs_srl = F.softmax(logits_srl, dim=1).cpu()
            # shape [num_srl_nodes, 2]

        # ent loss
        loss_ent = None # not all ans are an entity
        probs_ent = torch.tensor([], device=device)
        if logits_ent is None:
            loss_ent = None
            probs_ent = None
        else:        
            probs_ent = F.softmax(logits_ent, dim=1).cpu()
            # shape [num_ent_nodes, 2]

        # ans type
#         input_ans_type_classif = self.dropout_ans_type(torch.cat((graph_emb['AT'], graph_emb['query']), dim=1))
#         logits_ans_type = self.ans_type_classifier(input_ans_type_classif).view(1, -1)
#         prediction_ans_type = torch.argmax(logits_ans_type, dim=1)
#         ans_type_label = graph.nodes['AT'].data['labels'].squeeze(0).to(device)
#         loss_ans_type = loss_fn_ans_type(logits_ans_type, ans_type_label)

        return ({'sent': {'probs': probs_sent},
                'srl': {'probs': probs_srl},
                'ent': {'probs': probs_ent},
                },
                graph_emb)
    
    def graph_initial_embedding(self, graph, bert_context_emb):
        '''
        Inputs:
            - graph
            - bert_context_emb shape [1, #max len, 768]
        '''
        input_gru = bert_context_emb[0].view(-1, 1, dict_params['in_feats'])
        encoder_output, encoder_hidden = self.bigru(input_gru)
        encoder_output = encoder_output.view(-1, dict_params['in_feats']*2)
        graph_emb = {ntype : nn.Parameter(torch.Tensor(graph.number_of_nodes(ntype), dict_params['in_feats']))
                      for ntype in graph.ntypes if graph.number_of_nodes(ntype) > 0}
        for ntype in graph.ntypes:
            if graph.number_of_nodes(ntype) == 0:
                continue
            list_emb = []
            for (st, end) in graph.nodes[ntype].data['st_end_idx']:
                node_token_emb = encoder_output[st:end]
                left2right = node_token_emb[-1, :dict_params['in_feats']].view(-1, dict_params['in_feats'])
                right2left = node_token_emb[0, dict_params['in_feats']:].view(-1, dict_params['in_feats'])
                # concat
                concat_both_dir = torch.cat((left2right, right2left), dim=1)
                list_emb.append(concat_both_dir.squeeze(0))
            list_emb = torch.stack(list_emb, dim=0)
            graph_emb[ntype] = self.gru_aggregation(list_emb)
        return graph_emb
    
    def aggregate_emb(self, encoder_output):      
        left2right = encoder_output[-1, :dict_params['in_feats']].view(-1, dict_params['in_feats'])
        right2left = encoder_output[0, dict_params['in_feats']:].view(-1, dict_params['in_feats'])
        # concat
        concat_both_dir = torch.cat((left2right, right2left), dim=1)
        # create the emb
        emb = self.gru_aggregation(concat_both_dir).squeeze(0)
        return emb 
#         return torch.mean(token_emb, dim = 0)
    
    def sample_sent_nodes(self, graph):
        list_sent_nodes = graph.nodes('sent')
        sent_nodes_labels = graph.nodes['sent'].data['labels']
        # shape [num sent nodes x 1]
        supporting_sent_mask = sent_nodes_labels == torch.ones((sent_nodes_labels.shape))
        # shape [num sent nodes x 1] with values True or False
        supporting_sent_idx = [idx for idx, class_ in enumerate(supporting_sent_mask) if class_]
        # list with the idx of supporting sent
        non_supp_sent_idx = [i for i, non_supp_sent_node in enumerate(~supporting_sent_mask) if non_supp_sent_node]
        # list with the idx of non supporting sent
        num_supp_sent = len(supporting_sent_idx)
        non_supp_sent_idx_sample = random.sample(non_supp_sent_idx, min(num_supp_sent, 
                                                                        len(non_supp_sent_idx)))
        sent_sample_idx = supporting_sent_idx + non_supp_sent_idx_sample
        sent_sample_idx.sort()

        return sent_sample_idx

    def sample_srl_nodes(self, graph):
        list_srl_nodes = graph.nodes('srl')
        srl_nodes_labels = graph.nodes['srl'].data['labels']
        # shape [num labels x 1]
        supporting_srl_mask = srl_nodes_labels == torch.ones((srl_nodes_labels.shape))
        # shape [num labels x 1] with values True or False
        supporting_srl_idx = [idx for idx, class_ in enumerate(supporting_srl_mask) if class_]
        # list with the idx of supporting srl
        non_supp_srl_idx = [i for i, non_supp_srl_node in enumerate(~supporting_srl_mask) if non_supp_srl_node]
        # list with the idx of non supporting srl
        num_supp_srl = len(supporting_srl_idx)
        non_supp_srl_idx_sample = random.sample(non_supp_srl_idx, min(num_supp_srl, 
                                                                        len(non_supp_srl_idx)))
        srl_sample_idx = supporting_srl_idx + non_supp_srl_idx_sample
        srl_sample_idx.sort()

        return srl_sample_idx
    
    def sample_ent_nodes(self, graph):
        if 'ent' not in graph.ntypes:
            return []
        list_ent_nodes = graph.nodes('ent')
        ent_nodes_labels = graph.nodes['ent'].data['labels']
        # shape [num labels x 1]
        supporting_ent_mask = ent_nodes_labels == torch.ones((ent_nodes_labels.shape))
        # shape [num labels x 1] with values True or False
        supporting_ent_idx = [idx for idx, class_ in enumerate(supporting_ent_mask) if class_]
        # list with the idx of supporting srl
        non_supp_ent_idx = [i for i, non_supp_ent_node in enumerate(~supporting_ent_mask) if non_supp_ent_node]
        # list with the idx of non supporting srl
        num_supp_ent = len(supporting_ent_idx)
        non_supp_ent_idx_sample = random.sample(non_supp_ent_idx, min(num_supp_ent, 
                                                                      len(non_supp_ent_idx)))
        ent_sample_idx = supporting_ent_idx + non_supp_ent_idx_sample
        ent_sample_idx.sort()

        return ent_sample_idx
    
    
    def update_sequence_outputs(self, sequence_output, graph, graph_emb):
        list_edges = []
        offset_node = sequence_output.shape[1]
        for node_idx, (st, end) in enumerate(graph.ndata['st_end_idx']):
            u = node_idx + offset_node
            list_edges.extend([(u, t) for t in range(st, end)])
        # embeddign of the new graph: bert token embs + HS graph
        g2d_graph_emb = torch.cat((sequence_output.squeeze(0), graph_emb))
        # graph creation for the self attention
        g2d_graph = dgl.DGLGraph()
        g2d_graph.add_nodes(g2d_graph_emb.shape[0])
        src, dst = tuple(zip(*list_edges))
        # dgl is directional
        g2d_graph.add_edges(src, dst)
        g2d_graph_emb = self.graph2token_attention(g2d_graph, g2d_graph_emb)
        return g2d_graph_emb[0:offset_node].unsqueeze(0)
    
    def span_prediction(self, sequence_output, start_positions, end_positions):
        logits = self.qa_outputs(sequence_output)
        start_logits, end_logits = logits.split(1, dim=-1)
        start_logits = start_logits.squeeze(-1)
        end_logits = end_logits.squeeze(-1)

        total_loss = None
        if ((start_positions is not None and end_positions is not None) and
            (start_positions != -1 and end_positions != -1)):
            loss_fct = nn.CrossEntropyLoss()
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2
        return (total_loss, start_logits, end_logits)

class Validation():

    def __init__(self, model, dataset, validation_dataloader,
                 tensor_input_ids, tensor_attention_masks, tensor_token_type_ids):
        self.model = model
        self.model.eval()
        self.dataset = dataset
        self.validation_dataloader = validation_dataloader
        self.tensor_input_ids = tensor_input_ids
        self.tensor_attention_masks = tensor_attention_masks
        self.tensor_token_type_ids = tensor_token_type_ids
        self.tokenizer = BertTokenizer.from_pretrained(pretrained_weights)

    def get_answer_predictions(self, dict_ins2dict_doc2pred):
        output_pred_sp = {}
        output_predictions_ans = {}
        for step, b_graph in enumerate(tqdm(self.validation_dataloader)): 
            with torch.no_grad():
                output = self.model(b_graph,
                               input_ids=self.tensor_input_ids[step].unsqueeze(0).to(device),
                               attention_mask=self.tensor_attention_masks[step].unsqueeze(0).to(device),
                               token_type_ids=self.tensor_token_type_ids[step].unsqueeze(0).to(device), 
                               train=False)
            #answer
            predicted_ans = ""
            predicted_ans = self.__get_pred_ans_str(self.tensor_input_ids[step], output)
            _id = self.dataset[step]['_id']
            output_predictions_ans[_id] = predicted_ans
            #sp
            prediction_sent = torch.argmax(output['sent']['probs'], dim=1)
            sent_num = 0
            dict_sent_num2str = dict()
            for doc_idx, (doc_title, doc) in enumerate(self.dataset[step]['context']):
                if dict_ins2dict_doc2pred[step][doc_idx] == 1:
                    for i, sent in enumerate(doc):
                        dict_sent_num2str[sent_num] = {'sent': i, 'doc_title': doc_title}
                        sent_num += 1
            output_pred_sp[_id] = []
            for i, pred in enumerate(prediction_sent):
                if pred == 1:
                    output_pred_sp[_id].append([dict_sent_num2str[i]['doc_title'],
                                                dict_sent_num2str[i]['sent']])
        return {'answer': output_predictions_ans, 'sp': output_pred_sp}

    def __get_pred_ans_str(self, input_ids, output):
        st = torch.argmax(output['span']['start_logits'], dim=1).item()
        end = torch.argmax(output['span']['end_logits'], dim=1).item()
        return self.tokenizer.decode(input_ids[st:end])