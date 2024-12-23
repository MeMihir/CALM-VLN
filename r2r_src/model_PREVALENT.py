# Recurrent VLN-BERT, 2020, by Yicong.Hong@anu.edu.au

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
from param import args
import torch.nn.functional as F

from vlnbert.vlnbert_init import get_vlnbert_models

class VLNBERT(nn.Module):
    def __init__(self, feature_size=2048+128):
        super(VLNBERT, self).__init__()
        print('\nInitalizing the VLN-BERT model ...')

        self.vln_bert = get_vlnbert_models(args, config=None)  # initialize the VLN-BERT
        self.vln_bert.config.directions = 4  # a preset random number

        hidden_size = self.vln_bert.config.hidden_size
        layer_norm_eps = self.vln_bert.config.layer_norm_eps

        self.action_state_project = nn.Sequential(
            nn.Linear(hidden_size+args.angle_feat_size, hidden_size), nn.Tanh())
        self.action_LayerNorm = BertLayerNorm(hidden_size, eps=layer_norm_eps)

        self.drop_env = nn.Dropout(p=args.featdropout)
        self.img_projection = nn.Linear(feature_size, hidden_size, bias=True)
        self.cand_LayerNorm = BertLayerNorm(hidden_size, eps=layer_norm_eps)

        self.vis_lang_LayerNorm = BertLayerNorm(hidden_size, eps=layer_norm_eps)
        self.state_proj = nn.Linear(hidden_size*2, hidden_size, bias=True)
        self.state_LayerNorm = BertLayerNorm(hidden_size, eps=layer_norm_eps)
        # self.dropout = nn.Dropout(config.hidden_dropout_prob)
        self.mc_dropout_samples = 10  # Number of MC dropout samples
        self.mc_dropout = False  # Flag to control MC dropout

        
    def enable_dropout(self):
        """Enable dropout during inference"""
        for module in self.modules():
            if isinstance(module, nn.Dropout):
                module.train()
    
    def monte_carlo_forward(self, mode, sentence, token_type_ids=None,
                          attention_mask=None, lang_mask=None, vis_mask=None,
                          position_ids=None, action_feats=None, pano_feats=None, cand_feats=None):
        """Perform multiple forward passes with dropout enabled"""
        self.mc_dropout = True
        self.enable_dropout()
        
        outputs = []
        with torch.no_grad():
            for _ in range(self.mc_dropout_samples):
                output = self.forward(mode, sentence, token_type_ids, attention_mask, 
                                    lang_mask, vis_mask, position_ids, action_feats, 
                                    pano_feats, cand_feats)
                outputs.append(output)
        
        self.mc_dropout = False
        return outputs

    def forward(self, mode, sentence, token_type_ids=None,
                attention_mask=None, lang_mask=None, vis_mask=None,
                position_ids=None, action_feats=None, pano_feats=None, cand_feats=None):

        if self.mc_dropout:
            self.enable_dropout()

        if mode == 'language':
            init_state, encoded_sentence = self.vln_bert(mode, sentence, attention_mask=attention_mask, lang_mask=lang_mask,)

            return init_state, encoded_sentence

        elif mode == 'visual':

            state_action_embed = torch.cat((sentence[:,0,:], action_feats), 1)
            state_with_action = self.action_state_project(state_action_embed)
            state_with_action = self.action_LayerNorm(state_with_action)
            state_feats = torch.cat((state_with_action.unsqueeze(1), sentence[:,1:,:]), dim=1)

            cand_feats[..., :-args.angle_feat_size] = self.drop_env(cand_feats[..., :-args.angle_feat_size])

            # logit is the attention scores over the candidate features
            h_t, logit, attended_language, attended_visual, confidence_score = self.vln_bert(mode, state_feats,
                attention_mask=attention_mask, lang_mask=lang_mask, vis_mask=vis_mask, img_feats=cand_feats)

            # update agent's state, unify history, language and vision by elementwise product
            vis_lang_feat = self.vis_lang_LayerNorm(attended_language * attended_visual)
            state_output = torch.cat((h_t, vis_lang_feat), dim=-1)
            state_proj = self.state_proj(state_output)
            state_proj = self.state_LayerNorm(state_proj)

            return state_proj, logit, confidence_score

        else:
            ModuleNotFoundError


class BertLayerNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-12):
        """Construct a layernorm module in the TF style (epsilon inside the square root).
        """
        super(BertLayerNorm, self).__init__()
        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.bias = nn.Parameter(torch.zeros(hidden_size))
        self.variance_epsilon = eps

    def forward(self, x):
        u = x.mean(-1, keepdim=True)
        s = (x - u).pow(2).mean(-1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.variance_epsilon)
        return self.weight * x + self.bias


class Critic(nn.Module):
    def __init__(self):
        super(Critic, self).__init__()
        self.state2value = nn.Sequential(
            nn.Linear(768, 512),
            nn.ReLU(),
            nn.Dropout(args.dropout),
            nn.Linear(512, 1),
        )

    def forward(self, state):
        return self.state2value(state).squeeze()
