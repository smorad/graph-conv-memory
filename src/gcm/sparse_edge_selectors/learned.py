import torch
from typing import List, Any, Tuple, Union

from torchtyping import TensorType, patch_typeguard  # type: ignore
from typeguard import typechecked  # type: ignore
from gcm import util

patch_typeguard()


class LearnedEdge(torch.nn.Module):
    """Add temporal edges to the edge list"""

    def __init__(
        self, 
        # Feature size of a graph node
        input_size: int = 0,
        # Number of edges to sample per node (upper bounds the
        # number of edges for each node)
        num_edge_samples: int = 5,
        # Whether to randomly sample using gumbel softmax
        # or use sparsemax
        deterministic: bool = False,
        # Only consider edges to vertices in a fixed-size window
        # this reduces memory usage but prohibits edges to nodes outside
        # the window. Use None for no window (all possible edges)
        window: Union[int, None] = None
    ):
        super().__init__()
        self.deterministic = deterministic
        self.num_edge_samples = num_edge_samples
        # This MUST be done here
        # if initialized in forward model does not learn...
        self.edge_network = self.build_edge_network(input_size)
        if deterministic:
            self.sm = util.Spardmax()
        self.ste = util.StraightThroughEstimator()
        self.window = window

    def build_edge_network(self, input_size: int) -> torch.nn.Sequential:
        """Builds a network to predict edges.
        Network input: (i || j)
        Network output: logits(edge(i,j))
        """
        return torch.nn.Sequential(
            torch.nn.Linear(2 * input_size, input_size),
            torch.nn.ReLU(),
            torch.nn.LayerNorm(input_size),
            torch.nn.Linear(input_size, input_size),
            torch.nn.ReLU(),
            torch.nn.LayerNorm(input_size),
            torch.nn.Linear(input_size, 1),
        )

    @typechecked
    def forward(
        self,
        nodes: TensorType["B", "N", "feat", float],  # type: ignore # noqa: F821
        T: TensorType["B", int],  # type: ignore # noqa: F821
        taus: TensorType["B", int],  # type: ignore # noqa: F821
        B: int,
        ) -> TensorType["B", "N", "N", float, torch.sparse_coo]:  # type: ignore # noqa: F821

        """
        # No edges to create
        if (T + taus).max() <= 1:
            return torch.zeros((2, 0), dtype=torch.long, device=nodes.device), torch.zeros((0), dtype=torch.long, device=nodes.device)
        """

        if self.edge_network[0].weight.device != nodes.device:
            self.edge_network = self.edge_network.to(nodes.device)

        # TODO: use window

        # Do for all batches at once
        #
        # Construct indices denoting all edges, which we sample from
        # Note that we only want to sample incoming edges from nodes T to T + tau
        edge_idx = []
        for b in range(B):
            # Use windows to reduce size, in case the graph is too big
            if self.window is not None:
                window_min_idx = max(0, T[b] - self.window)
            else:
                window_min_idx = 0
            edge = torch.tril_indices(
                T[b] + taus[b], T[b] + taus[b], offset=-1, dtype=torch.long,
            )
            window_mask = edge[1] >= window_min_idx
            # Remove edges outside of window
            edge = edge[:, window_mask]


            batch = b * torch.ones(edge[-1].shape[-1], device=nodes.device, dtype=torch.long)
            edge_idx.append(torch.cat((batch.unsqueeze(0), edge), dim=0))

        # Shape [3, N] denoting batch, sink, source
        # these indices denote nodes pairs being fed to network
        edge_idx = torch.cat(edge_idx, dim=-1)
        batch_idx, sink_idx, source_idx = edge_idx.unbind()
        # Feed node pairs to network
        sink_nodes = nodes[batch_idx, sink_idx]
        source_nodes = nodes[batch_idx, source_idx]
        network_input = torch.cat((sink_nodes, source_nodes), dim=-1)
        # Logits is of shape [N]
        logits = self.edge_network(network_input).squeeze()
        # TODO rather than sparse to dense conversion, implement
        # a sparse gumbel softmax
        gs_input = torch.empty(
            (batch_idx.max() + 1, sink_idx.max() + 1, source_idx.max() + 1),
            device=nodes.device, dtype=torch.float
        ).fill_(torch.finfo(torch.float).min)
        gs_input[batch_idx, sink_idx, source_idx] = logits
        # Draw num_samples from gs distribution
        gs_input = gs_input.repeat(self.num_edge_samples, 1, 1, 1)
        soft = torch.nn.functional.gumbel_softmax(gs_input, hard=True, dim=3)
        # Clamp adj to 1
        edges = self.ste(soft.sum(dim=0))
        adj_idx = edges.nonzero().T
        adj_vals = edges[adj_idx.unbind()]
        # Remove self edges
        # TODO: do we want to keep so the network can learn
        # "no edges"?
        mask = adj_idx[1] != adj_idx[2]
        adj_idx = adj_idx[:,mask]
        adj_vals = adj_vals[mask]

        adj = torch.sparse_coo_tensor(
            indices=adj_idx,
            values=adj_vals,
            size=(B, nodes.shape[1], nodes.shape[1])
        )
        return adj





        




        for b in range(B):
            # Construct all valid edge combinations
            edge_idx = torch.tril_indices(
                T[b] + taus[b], T[b] + taus[b], offset=-1
            )
            # Don't evaluate incoming edges for the T entries, as we are only
            # interested in incoming data for T + tau
            sink_idx, source_idx = edge_idx[:, edge_idx[0] > T[b]] 

            sink_nodes = nodes[b, sink_idx]
            source_nodes = nodes[b, source_idx]

            # Thru network for logits
            network_input = torch.cat((sink_nodes, source_nodes), dim=-1)
            logits = self.edge_network(network_input).squeeze()

            # Logits to probabilities via gumbel softmax
            gs_in = logits.repeat(self.num_edge_samples, 1, 1)
            soft = torch.nn.functional.gumbel_softmax(gs_in, hard=True)
            # Store gumbel softmax output so we can propagate their gradients
            # thru weights/values in the adj
            sampled_edge_grad_path = self.ste(soft.sum(dim=0)).squeeze(0)
            sampled_edge_idx = sampled_edge_grad_path.nonzero().squeeze(1)

            # Add [B, source, sink] edge indices
            adj_idx = torch.stack((
                b * torch.ones(sampled_edge_idx.shape[-1]),
                sink_idx[sampled_edge_idx],
                source_idx[sampled_edge_idx],
            ))
            adj_idxs.append(adj_idx)

            # Gradients are stored here
            adj_vals.append(sampled_edge_grad_path[sampled_edge_idx])


        indices = torch.cat(adj_idxs, dim=-1)
        values = torch.cat(adj_vals)

        adj = torch.sparse_coo_tensor(
            indices=indices,
            values=values,
            size=(B, nodes.shape[1], nodes.shape[1])
        )
        return adj



        #logits = self.edge_network(net_in).squeeze() 
        # Logits are indexed as [B, T*tau
        

            
