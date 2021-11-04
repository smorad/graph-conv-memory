import torch
import numpy as np
import ray
import torch_geometric
import sparsemax
from typing import Tuple, List


class STEFunction(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        return (input > 0).float()

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output
        #return torch.nn.functional.hardtanh(grad_output)

class StraightThroughEstimator(torch.nn.Module):
    def __init__(self):
        super(StraightThroughEstimator, self).__init__()

    def forward(self, x):
        x = STEFunction.apply(x)
        return x



class Spardmax(torch.nn.Module):
    """A hard version of sparsemax"""
    def __init__(self, dim=-1, cutoff=0):
        super().__init__()
        self.dim = dim
        self.cutoff = cutoff
        self.sm = sparsemax.Sparsemax(dim)
    
    def forward(self, x):
        # Straight through.
        y_soft = self.sm(x)
        y_hard = (y_soft > self.cutoff).float()
        return y_hard - y_soft.detach() + y_soft

class Hardmax(torch.nn.Module):
    def __init__(self, dim=-1, cutoff=0.2):
        super().__init__()
        self.dim = dim
        self.cutoff = cutoff
        self.sm = torch.nn.Softmax(dim)
    
    def forward(self, x):
        # Straight through.
        y_soft = self.sm(x)
        y_hard = (y_soft > self.cutoff).float()
        return y_hard - y_soft.detach() + y_soft


class SparsegenLin(torch.nn.Module):
    def __init__(self, lam, normalized=True):
        super().__init__()
        self.lam = lam
        self.normalized = normalized

    def forward(self, z):
        bs = z.data.size()[0]
        dim = z.data.size()[1]
        # z = input.sub(torch.mean(input,dim=1).repeat(1,dim))
        dtype = torch.FloatTensor
        #z = input.type(dtype)

        #sort z
        z_sorted = torch.sort(z, descending=True)[0]

        #calculate k(z)
        z_cumsum = torch.cumsum(z_sorted, dim=1)
        k = torch.autograd.Variable(torch.arange(1, dim + 1, device=z.device).unsqueeze(0).repeat(bs,1))
        z_check = torch.gt(1 - self.lam + k * z_sorted, z_cumsum)

        # because the z_check vector is always [1,1,...1,0,0,...0] finding the
        # (index + 1) of the last `1` is the same as just summing the number of 1.
        k_z = torch.sum(z_check.float(), 1)

        #calculate tau(z)
        tausum = torch.sum(z_check.float() * z_sorted, 1)
        tau_z = (tausum - 1 + self.lam) / k_z
        prob = z.sub(tau_z.view(bs,1).repeat(1,dim)).clamp(min=0)
        if self.normalized:
               prob /= (1-self.lam)
        return prob


class Spardgen(torch.nn.Module):
    """A hard version of sparsegen-lin"""
    def __init__(self, dim=-1, lam=0.75):
        super().__init__()
        self.dim = dim
        self.sm = SparsegenLin(lam)
    
    def forward(self, x):
        # Only takes up to 2ds so reshape
        x_in = x.reshape(-1, x.shape[-1])
        # Straight through.
        y_soft = self.sm(x_in).reshape(x.shape)
        y_hard = (y_soft != 0).float()
        return y_hard - y_soft.detach() + y_soft

def get_nonpadded_idxs(T, taus, B):
    """Get the non-padded indices of a zero-padded
    batch of observations. In other words, get only valid elements and discard
    the meaningless zeros."""
    dense_B_idxs = torch.cat([torch.ones(taus[b], device=T.device, dtype=torch.long) * b for b in range(B)])
    # These must not be offset by T like get_new_node_idxs
    dense_tau_idxs = torch.cat([torch.arange(taus[b], device=T.device) for b in range(B)])
    return dense_B_idxs, dense_tau_idxs
    

@torch.jit.script
def get_new_node_idxs(T: torch.Tensor, taus: torch.Tensor, B: int):
    """Given T and tau tensors, return indices matching batches to taus.
    These tell us which elements in the node matrix we have just added
    during this iteration, and organize them by batch. 

    E.g. 
    g_idxs = torch.where(B_idxs == 0)
    zeroth_graph_new_nodes = nodes[B_idxs[g_idxs], tau_idxs[g_idxs]] 
    """
    # TODO: batch this using b_idx and cumsum
    B_idxs = torch.cat([torch.ones(taus[b], device=T.device, dtype=torch.long) * b for b in range(B)])
    tau_idxs = torch.cat([torch.arange(T[b], T[b] + taus[b], device=T.device) for b in range(B)])
    return B_idxs, tau_idxs

@torch.jit.script
def get_valid_node_idxs(T: torch.Tensor, taus: torch.Tensor, B: int):
    """Given T and tau tensors, return indices matching batches to taus.
    These tell us which elements in the node matrix are valid for convolution,
    and organize them by batch. 

    E.g. 
    g_idxs = torch.where(B_idxs == 0)
    zeroth_graph_all_nodes = nodes[B_idxs[g_idxs], tau_idxs[g_idxs]] 
    """
    # TODO: batch this using b_idx and cumsum
    B_idxs = torch.cat([torch.ones(T[b] + taus[b], device=T.device, dtype=torch.long) * b for b in range(B)])
    tau_idxs = torch.cat([torch.arange(0, T[b] + taus[b], device=T.device) for b in range(B)])
    return B_idxs, tau_idxs

def to_dense(mx, T, taus, B):
    """Compute the dense version of mx.

    The output of sparse_gcm.forward returns mx of shape
    [B*taus, feat]. But in some cases (like rllib) we want
    to return a zero-padded tensor of shape [B, max(taus), feat]
    instead. This fn returns a zero-padded version of said tensor."""
    dense_B_idxs, dense_tau_idxs = get_nonpadded_idxs(T, taus, B)
    dense_mx = torch.zeros((B, taus.max(), mx.shape[-1]), device=mx.device)
    dense_mx[dense_B_idxs, dense_tau_idxs] = mx

    return dense_mx


@torch.jit.script
def get_batch_offsets(T: torch.Tensor, taus: torch.Tensor):
    """Get node offsets into flattened tensor"""
    # Initial offset is zero, not T + tau, roll into place
    batch_offsets = (T + taus).cumsum(dim=0).roll(1,0)
    batch_offsets[0] = 0
    return batch_offsets

    
'''
def unique(x, dim=None):
    """Unique elements of x and indices of those unique elements
    https://github.com/pytorch/pytorch/issues/36748#issuecomment-619514810

    e.g.

    unique(tensor([
        [1, 2, 3],
        [1, 2, 4],
        [1, 2, 3],
        [1, 2, 5]
    ]), dim=0)
    => (tensor([[1, 2, 3],
                [1, 2, 4],
                [1, 2, 5]]),
        tensor([0, 1, 3]))
    """
    unique, inverse = torch.unique(
        x, sorted=True, return_inverse=True, dim=dim)
    perm = torch.arange(inverse.size(0), dtype=inverse.dtype,
                        device=inverse.device)
    inverse, perm = inverse.flip([0]), perm.flip([0])
    return unique, inverse.new_empty(unique.size(dim)).scatter_(0, inverse, perm)

def first_available_edge_idx(edges):
    """Given edges with shape [B,2,E], return
    the first unused edge index for each batch"""
    padded_edges = (edges == -1).all(dim=1)
    batch_idx, edge_idx = torch.unbind(padded_edges.nonzero().T)
    # Expand each batch into an edge
    # and let torch coalesce handle the rest
    batch_idx = batch_idx.expand(2, -1)#.repeat(2,1)
    batch_idx_rp, edge_boundary_idx = torch_geometric.utils.coalesce(batch_idx, edge_idx, reduce='min')
    return batch_idx_rp[0], edge_boundary_idx
'''

def add_edges(edges, new_edges, weights, new_weights=None):
    """Add new edges of [B,2,NE] to edges [B,2,E] (and same with weights)"""
    for b in range(edges.shape[0]):
        ray.util.pdb.set_trace()
        free_start_idx = (edges[b][0] == -1).nonzero()[0]
        end_idx = free_start_idx + new_edges[b].shape[-1]
        edges[b, : , free_start_idx:end_idx] = new_edges[b]
        if new_weights is None:
            weights[b, free_start_idx:end_idx] = 1.0
        else:
            weights[b, free_start_idx:end_idx] = new_weights

    '''
    free_idx = (edges == -1).all(dim=1)
    num_edges = 
    edges[free_batch_idx

    #free_batch_idx, free_edge_idx = first_available_edge_idx(edges)
    '''


def pack_hidden(hidden, B, max_edges, edge_fill=-1, weight_fill=1.0):
    return _pack_hidden(*hidden, B, max_edges, edge_fill=-1, weight_fill=1.0)

def unpack_hidden(hidden, B):
    nodes, flat_edges, flat_weights, T, flat_B_idx = _unpack_hidden(*hidden, B)
    
    # The following can't be jitted, so it sits in this fn

    # Finally, remove duplicate edges and weights
    # but only if we have edges
    if flat_edges.numel() > 0:
        # Make sure idxs are removed alongside edges and weights
        flat_edges, [flat_weights, flat_B_idx] = torch_geometric.utils.coalesce(
            flat_edges, [flat_weights, flat_B_idx], reduce='min'
        )
    return nodes, flat_edges, flat_weights, T


@torch.jit.script
def _pack_hidden(
    nodes: torch.Tensor, 
    edges: torch.Tensor, 
    weights: torch.Tensor,
    T: torch.Tensor, 
    B: int, 
    max_edges: int, 
    edge_fill: int=-1, 
    weight_fill: float=1.0):
    """Converts the hidden states to a dense representation

    Unflatten edges from [2, k* NE] to [B, 2, max_edges].  In other words, prep
    edges and weights for dense transport (ray).

    Returns an updated hidden representation"""

    #nodes, edges, weights, T = hidden
    batch_ends = T.cumsum(dim=0)
    batch_starts = batch_ends.roll(1)
    batch_starts[0] = 0
    dense_edges = torch.zeros((B, 2, max_edges), dtype=torch.long).fill_(edge_fill)
    dense_weights = torch.zeros((B, 1, max_edges), dtype=torch.float).fill_(weight_fill)

    for b in range(B):
        source_mask = (batch_starts[b] <= edges[0]) * (edges[0] < batch_ends[b])
        sink_mask = (batch_starts[b] <= edges[1]) * (edges[1] < batch_ends[b])
        mask = source_mask * sink_mask

        # Only if we have edges
        if edges[:,mask].numel() > 0:
            num_edges = mask.shape[0]
            # More than max edges
            if num_edges > max_edges:
                truncate = num_edges - max_edges
                print(
                    f'Warning: {num_edges} edges greater than max edges {max_edges} '
                    f'dropping the first {truncate} edges'
                )
            batch_edges = edges[:,mask] - batch_starts[b]
            batch_weights = weights[mask]
            max_indices = min(batch_edges.shape[-1], max_edges)
            dense_edges[b,:, :max_indices] = batch_edges[:,:max_indices]
            dense_weights[b, 0, :max_indices] = batch_weights[:max_indices]

    return nodes, dense_edges, dense_weights, T


@torch.jit.script
def _unpack_hidden(
    nodes: torch.Tensor, 
    edges: torch.Tensor, 
    weights: torch.Tensor,
    T: torch.Tensor, 
    B: int):
    """Converts dense hidden states to a sparse representation
    
    Unflatten edges from [2, k* NE] to [B, 2, max_edges].  In other words, prep
    edges and weights for dense transport (ray).

    Returns edges [B,2,NE] and weights [B,1,NE]"""
    batch_offsets = T.cumsum(dim=0).roll(1)
    batch_offsets[0] = 0

    edge_offsets = batch_offsets.unsqueeze(-1).unsqueeze(-1).expand(-1,2,edges.shape[-1])
    offset_edges = edges + edge_offsets
    offset_edges_B_idx = torch.cat(
        [
            b * torch.ones(
                edges.shape[-1], device=edges.device, dtype=torch.long
            ) for b in range(B)
        ]
    )
    # Filter invalid edges (those that were < 0 originally)
    # Swap dims (B,2,NE) => (2,B,NE)
    mask = (offset_edges >= edge_offsets).permute(1,0,2)
    stacked_mask = (mask[0] & mask[1]).unsqueeze(0).expand(2,-1,-1)
    # Now filter edges, weights, and indices using masks
    # Careful, mask select will automatically flatten
    # so do it last, this squeezes from from (2,B,NE) => (2,B*NE)
    flat_edges = edges.permute(1,0,2).masked_select(stacked_mask).reshape(2,-1)
    flat_weights = weights.masked_select(stacked_mask[0]).flatten()
    flat_B_idx = offset_edges_B_idx.masked_select(stacked_mask[0].flatten())
        

    return nodes, flat_edges, flat_weights, T, flat_B_idx


def unflatten_edges_and_weights(edges, weights, max_edges, B, edge_fill=-1, weight_fill=1.0):
    """Unflatten edges from [2, k* NE] to [B, 2, max_edges].  In other words, prep
    edges and weights for dense transport (ray).

    Returns edges [B,2,NE] and weights [B,1,NE]"""
    batch_starts = get_batch_offsets(T, taus)
    batch_ends = (T + taus).cumsum(dim=0).roll(1,0)
    dense_edges = torch.zeros((B, 2, max_edges))._fill(edge_fill)
    dense_weights = torch.zeros((B, 1, max_edges))._fill(weight_fill)

    for b in range(B):
        mask = (batch_starts[b] < edges < batch_ends[b])
        source_mask = (batch_starts[b][0] < edges) * (edges < batch_ends[b][0])
        sink_mask = (batch_starts[b][1] < edges) * (edges < batch_ends[b][1])
        mask = source_mask * sink_mask

        dense_edges[b,:] = edges[:,mask]
        dense_weights[b] = weights[mask]
    """
    dense_edges = -1 * torch.ones(B, 2, max_edges)
    dense_edges.scatter_(0, edge_b_idx, edges)
    dense_weights = -1 * torch.ones(B, 1, max_edges)
    dense_weights = torch.scatter(0, edge_b_idx, weights, dense_weights)
    """
    return dense_edges, dense_weights


def flatten_edges_and_weights(edges, weights, T, taus, B):
    """Flatten edges from [B, 2, NE] to [2, k * NE], coalescing
    and removing invalid edges (-1). In other words, prep
    edges and weights for GNN ingestion.

    Returns flattened edges, weights, and corresponding
    batch indices"""
    batch_offsets = get_batch_offsets(T, taus)
    edge_offsets = batch_offsets.unsqueeze(-1).unsqueeze(-1).expand(-1,2,edges.shape[-1])
    offset_edges = edges + edge_offsets
    offset_edges_B_idx = torch.cat(
        [
            b * torch.ones(
                edges.shape[-1], device=edges.device, dtype=torch.long
            ) for b in range(B)
        ]
    )
    # Filter invalid edges (those that were < 0 originally)
    # Swap dims (B,2,NE) => (2,B,NE)
    mask = (offset_edges >= edge_offsets).permute(1,0,2)
    stacked_mask = (mask[0] & mask[1]).unsqueeze(0).expand(2,-1,-1)
    # Now filter edges, weights, and indices using masks
    # Careful, mask select will automatically flatten
    # so do it last, this squeezes from from (2,B,NE) => (2,B*NE)
    flat_edges = edges.permute(1,0,2).masked_select(stacked_mask).reshape(2,-1)
    flat_weights = weights.permute(1,0,2).masked_select(stacked_mask[0]).flatten()
    flat_B_idx = offset_edges_B_idx.masked_select(stacked_mask[0].flatten())
        

    # Finally, remove duplicate edges and weights
    # but only if we have edges
    if flat_edges.numel() > 0:
        # Make sure idxs are removed alongside edges and weights
        flat_edges, [flat_weights, flat_B_idx] = torch_geometric.utils.coalesce(
            flat_edges, [flat_weights, flat_B_idx], reduce='min'
        )

    return flat_edges, flat_weights, flat_B_idx


def flatten_nodes(nodes, T, taus, B):
    """Flatten nodes from [B, N, feat] to [B * N, feat] for ingestion
    by the GNN.

    Returns flattened nodes and corresponding batch indices"""
    batch_offsets = get_batch_offsets(T, taus)
    B_idxs, tau_idxs = get_valid_node_idxs(T, taus, B)
    flat_nodes = nodes[B_idxs, tau_idxs]
    # Extracting belief requires batch-tau indices (newly inserted nodes)
    # return these too
    # Flat nodes are ordered B,:T+tau (all valid nodes)
    # We want B,T:T+tau (new nodes), which is batch_offsets:batch_offsets + tau
    output_node_idxs = torch.cat(
        [
            torch.arange(
                batch_offsets[b], batch_offsets[b] + taus[b], device=nodes.device
            ) for b in range(B)
        ]
    )
    return flat_nodes, output_node_idxs




def flatten_batch(nodes, edges, weights, T, taus, B):
    """Squeeze node, edge, and weight batch dimensions into a single
    huge graph. Also deletes non-valid edge pairs"""
    b_idx = torch.arange(B, device=nodes.device)
    # Initial offset is zero, not T + tau, roll into place
    batch_offsets = (T + taus).cumsum(dim=0).roll(1,0)
    batch_offsets[0] = 0

    # Flatten edges
    num_flat_edges = edges[b_idx].shape[-1]
    edge_offsets = batch_offsets.unsqueeze(-1).unsqueeze(-1).expand(-1,2,num_flat_edges)
    offset_edges = edges + edge_offsets
    # Filter invalid edges (those that were < 0 originally)
    # Swap dims (B,2,NE) => (2,B,NE)
    mask = (offset_edges >= edge_offsets).permute(1,0,2)
    stacked_mask = (mask[0] & mask[1]).unsqueeze(0).expand(2,-1,-1)
    # Careful, mask select will automatically flatten
    # so do it last, this squeezes from from (2,B,NE) => (2,B*NE)
    flat_edges = edges.permute(1,0,2).masked_select(stacked_mask).reshape(2,-1)
    # Do the same with weights, which will be of size E
    flat_weights = weights.permute(1,0,2).masked_select(stacked_mask[0]).flatten()
    # Finally, remove duplicate edges and weights
    # but only if we have edges
    if flat_edges.numel() > 0:
        flat_edges, flat_weights = torch_geometric.utils.coalesce(flat_edges, flat_weights)

    # Flatten nodes
    B_idxs, tau_idxs = get_valid_node_idxs(T, taus, B)
    flat_nodes = nodes[B_idxs, tau_idxs]
    # Extracting belief requires batch-tau indices (newly inserted nodes)
    # return these too
    # Flat nodes are ordered B,:T+tau (all valid nodes)
    # We want B,T:T+tau (new nodes), which is batch_offsets:batch_offsets + tau
    output_node_idxs = torch.cat(
        [
            torch.arange(
                batch_offsets[b], batch_offsets[b] + taus[b], device=nodes.device
            ) for b in range(B)
        ]
    )

    return flat_nodes, flat_edges, flat_weights, output_node_idxs

    datalist = []
    for b in range(B):
        data_x = dirty_nodes[b, :T[b] + taus[b]]
        # Get only valid edges (-1 signifies invalid edge)
        mask = edges[b] > -1
        # Delete nonvalid edge pairs
        data_edge = edges[b, :, mask[0] & mask[1]]
        #data_edge = edges[b][edges[b] > -1].reshape(2,-1) #< T[b] + tau]
        datalist.append(torch_geometric.data.Data(x=data_x, edge_index=data_edge))
    batch = torch_geometric.data.Batch.from_data_list(datalist)

@torch.jit.script
def diff_or(tensors: List[torch.Tensor]):
    """Differentiable OR operation bewteen n-tuple of tensors
    Input: List[tensors in {0,1}]
    Output: tensor in {0,1}"""
    print("This seems to dilute gradients, dont use it")
    res = torch.zeros_like(tensors[0])
    for t in tensors:
        tmp = res.clone()
        res = tmp + t - tmp * t
    return res


@torch.jit.script
def diff_or2(tensors: List[torch.Tensor]):
    """Differentiable OR operation bewteen n-tuple of tensors
    Input: List[tensors in {0,1}]
    Output: tensor in {0,1}"""
    print("This seems to dilute gradients, dont use it")
    # This nice form is actually slower than the matrix mult form
    return 1 - (1 - torch.stack(tensors, dim=0)).prod(dim=0)

@torch.jit.script
def idxs_up_to_including_num_nodes(
    nodes: torch.Tensor, num_nodes: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor]:
    """Given nodes and num_nodes, returns idxs from nodes
    up to and including num_nodes. I.e.
    [batches, 0:num_nodes + 1]. Note the order is
    sorted by (batches, num_nodes + 1) in ascending order.

    Useful for getting all active nodes in the graph"""
    seq_lens = num_nodes.unsqueeze(-1)
    N = nodes.shape[1]
    N_idx = torch.arange(N, device=nodes.device).unsqueeze(0)
    N_idx = N_idx.expand(seq_lens.shape[0], N_idx.shape[1])
    # include the current node
    N_idx = torch.nonzero(N_idx <= num_nodes.unsqueeze(1))
    assert N_idx.shape[-1] == 2
    batch_idxs = N_idx[:, 0]
    node_idxs = N_idx[:, 1]

    return batch_idxs, node_idxs

@torch.jit.script
def idxs_up_to_num_nodes(
    adj: torch.Tensor, num_nodes: torch.Tensor
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Given num_nodes, returns idxs from adj
    up to but not including num_nodes. I.e.
    [batches, 0:num_nodes, num_nodes]. Note the order is
    sorted by (batches, num_nodes, 0:num_nodes) in ascending order.

    Useful for getting all actives adj entries in the graph"""
    seq_lens = num_nodes.unsqueeze(-1)
    N = adj.shape[-1]
    N_idx = torch.arange(N, device=adj.device).unsqueeze(0)
    N_idx = N_idx.expand(seq_lens.shape[0], N_idx.shape[1])
    # Do not include the current node
    N_idx = torch.nonzero(N_idx < num_nodes.unsqueeze(1))
    assert N_idx.shape[-1] == 2
    batch_idxs = N_idx[:, 0]
    past_idxs = N_idx[:, 1]
    curr_idx = num_nodes[batch_idxs]

    return batch_idxs, past_idxs, curr_idx
