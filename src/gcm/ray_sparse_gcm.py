import torch
import numpy as np
import gym
from torch import nn
from typing import Union, Dict, List, Tuple, Any
import ray
from ray.rllib.models.torch.torch_modelv2 import TorchModelV2
from ray.rllib.models.torch.fcnet import FullyConnectedNetwork as TorchFC
from ray.rllib.models.torch.misc import SlimFC, normc_initializer
from ray.rllib.models.torch.recurrent_net import RecurrentNetwork
from ray.rllib.utils.typing import ModelConfigDict, TensorType
from ray.rllib.policy.sample_batch import SampleBatch
from ray.rllib.policy.view_requirement import ViewRequirement
from ray.rllib.utils.torch_ops import one_hot
from ray.rllib.policy.rnn_sequencing import add_time_dimension

import torch_geometric
from torch_geometric.data import Data, Batch
from gcm.gcm import DenseGCM, PositionalEncoding, RelativePositionalEncoding
from gcm.sparse_gcm import SparseGCM
from gcm import util


class RaySparseGCM(TorchModelV2, nn.Module):
    DEFAULT_CONFIG = {
        # Maximum number of nodes in a graph
        "graph_size": 32,
        # Maximum number of edges per graph per batch
        "max_edges": 5,
        # Input size to the GNN. Make sure your first gnn layer
        # has this many input channels
        "gnn_input_size": 64,
        # Number of output channels of the GNN. This feeds into the logits
        # and value function layers
        "gnn_output_size": 64,
        # GNN model that takes x, edge_index, weights
        # Note that input will be reshaped by a linear layer
        # to gnn_input_size
        "gnn": torch_geometric.nn.Sequential(
            "x, edge_index, weights, B, N",
            [
                (torch_geometric.nn.GraphConv(64, 64), "x, edge_index -> x"),
                torch.nn.Tanh(),
                (torch_geometric.nn.GraphConv(64, 64), "x, edge_index -> x"),
                torch.nn.Tanh(),
            ],
        ),
        # Torch.nn.module used for determining edges between nodes.
        # You can chain multiple modules together use
        # torch_geometric.nn.Sequential
        "edge_selectors": None,
        # Same as edge selectors, but called after reprojection
        # and positional encoding. Only use non-human (learned) edges here
        # as they are no longer in a human readable form
        "aux_edge_selectors": None,
        # Whether edge weights are used. This should be false unless using
        # bernoulli edges
        "edge_weights": False,
        # Optional network that processes observations before
        # the GNN. May allow for learning representations that
        # aggregate better. Note the input to the preprocessor will
        # already be of shape "gnn_input_size"
        # Note that the node preprocessor will run after observations are
        # inserted in the graph. This means the observations can be
        # reconstructed at the expense of greater memory usage compared
        # to the preprocessor
        "preprocessor": None,
        # Whether to train the preprocessor or freeze the weights
        "preprocessor_frozen": False,
        "pre_preprocessor": None,
        # Whether the prev action should be placed in the observation nodes
        "use_prev_action": False,
        # Whether to use positional encoding (ala transformer) in the GNN
        # False for none, 'cat' for concatenate encoding to feature vector,
        # and 'add' for sum encoding with feature vector
        "positional_encoding": None,
        # if 'cat', how many dimensions should be reserved for positional
        # encoding
        "positional_encoding_dim": 4,
    }

    def __init__(
        self,
        obs_space: gym.spaces.Space,
        action_space: gym.spaces.Space,
        num_outputs: int,
        model_config: ModelConfigDict,
        name: str,
        **custom_model_kwargs,
    ):
        TorchModelV2.__init__(
            self, obs_space, action_space, num_outputs, model_config, name
        )
        nn.Module.__init__(self)
        self.num_outputs = num_outputs
        self.obs_dim = gym.spaces.utils.flatdim(obs_space)
        self.act_space = action_space
        self.act_dim = gym.spaces.utils.flatdim(action_space)
        # edge selectors must be attrib of torch.nn.module
        # so edge_selectors get self.training, etc.

        for k in custom_model_kwargs:
            assert k in self.DEFAULT_CONFIG, f"Invalid config key {k}"
        self.cfg = dict(self.DEFAULT_CONFIG, **custom_model_kwargs)
        self.input_dim = self.obs_dim
        if self.cfg["use_prev_action"]:
            self.input_dim += self.act_dim
            self.view_requirements["prev_actions"] = ViewRequirement(
                "actions", space=self.action_space, shift=-1
            )

        self.build_network(self.cfg)
        print("Full GCM network is:", self)

        self.cur_val = None

    def build_network(self, cfg):
        """Builds the GNN and MLPs based on config"""
        pp = torch.nn.Linear(self.input_dim, cfg["gnn_input_size"])
        pe = None
        if cfg["positional_encoding"]:
            pe = PositionalEncoding(
                max_len=self.cfg["graph_size"],
                mode=cfg["positional_encoding"],
                cat_dim=cfg["positional_encoding_dim"],
            )

        if cfg["preprocessor"]:
            if cfg["preprocessor_frozen"]:
                for param in cfg["preprocessor"].parameters():
                    param.requires_grad = False
            pp = torch.nn.Sequential(pp, cfg["preprocessor"])

        self.gcm = SparseGCM(
            gnn=cfg["gnn"],
            max_edges=cfg["max_edges"],
            preprocessor=pp,
            edge_selectors=self.cfg["edge_selectors"],
            aux_edge_selectors=self.cfg["aux_edge_selectors"],
            positional_encoder=pe,
        )

        self.logit_branch = SlimFC(
            in_size=cfg["gnn_output_size"],
            out_size=self.num_outputs,
            activation_fn=None,
            initializer=normc_initializer(0.01),
        )

        self.value_branch = SlimFC(
            in_size=cfg["gnn_output_size"],
            out_size=1,
            activation_fn=None,
            initializer=normc_initializer(0.01),
        )

    def get_initial_state(self):
        nodes = torch.zeros((self.cfg["graph_size"], self.input_dim))
        # If these are type==long, they become np arrays instead of torch...
        edges = torch.zeros((2, self.cfg["max_edges"]))
        weights = torch.zeros((1, self.cfg["max_edges"]))

        T = torch.tensor(0, dtype=torch.long)
        state = [nodes, edges, weights, T]

        return state

    def value_function(self):
        assert self.cur_val is not None, "must call forward() first"
        return self.cur_val

    def forward(
        self,
        input_dict: Dict[str, TensorType],
        state: List[TensorType],
        seq_lens: TensorType,
    ) -> Tuple[TensorType, List[TensorType]]:

        if self.cfg["use_prev_action"]:
            prev_acts = one_hot(input_dict["prev_actions"].float(), self.act_space)
            prev_acts = prev_acts.reshape(-1, self.act_dim)
            flat = torch.cat((input_dict["obs_flat"], prev_acts), dim=-1)
        else:
            flat = input_dict["obs_flat"]

        dense = add_time_dimension(
            flat, 
            max_seq_len=seq_lens.max(), 
            framework="torch"
        )
        # Batch and Time
        # Forward expects outputs as [B, t, logits]
        # TODO: ppo sequencing is broken (rllib bug not ours)
        B = dense.shape[0]
        t = dense.shape[1]
        # Sometimes numpy sometimes tensor...
        if type(seq_lens) == np.ndarray:
            taus = torch.from_numpy(seq_lens).to(dense.device)
        else:
            taus = seq_lens

        nodes, edges, weights, T = state
        edges, T = edges.long(), T.long()

        # Push thru pre-gcm layers
        hidden = (nodes, edges, weights, T)
        # We have a zero-padded dense input
        # Thru GCM, output is flattened and ordered shape [B*tau, feat]
        out, hidden = self.gcm(dense, taus, hidden)
        logits = self.logit_branch(out)
        values = self.value_branch(out)

        # GCM output is [B*tau, feat], but ray wants it packed to max(seq_len)
        # should zero-padded to size [B * max(taus), feat]
        dense_B_idxs, dense_tau_idxs = util.get_nonpadded_idxs(T, taus, B)

        padded_logits = torch.zeros((B, taus.max(), logits.shape[-1]), device=logits.device)
        padded_logits[dense_B_idxs, dense_tau_idxs] = logits
        padded_logits = padded_logits.reshape(B * t, -1)

        padded_values = torch.zeros((B, taus.max(), values.shape[-1]), device=values.device)
        padded_values[dense_B_idxs, dense_tau_idxs] = values
        padded_values = padded_values.reshape(B * t)

        self.cur_val = padded_values

        state = list(hidden)
        return padded_logits, state