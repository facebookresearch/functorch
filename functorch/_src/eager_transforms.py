import torch
from functools import partial, wraps
import collections
import torch.nn as nn
import torch.nn.functional as F
from torch.utils._pytree import tree_flatten, tree_unflatten
from .pytree_hacks import tree_map, tree_map_
import gc

from .vmap import vmap
from .make_functional import make_functional, make_functional_with_buffers

from functorch._C import (
    _wrap_for_grad,
    _unwrap_for_grad,
    _grad_increment_nesting,
    _grad_decrement_nesting,
)

def _create_differentiable(inps, level=None):
    def create_differentiable(x):
        if isinstance(x, torch.Tensor):
            return x.requires_grad_()
        raise ValueError(f'Thing passed to transform API must be Tensor,'
                         f'got {type(x)}')
    return tree_map(create_differentiable, inps)

def _undo_create_differentiable(inps, level=None):
    def unwrap_tensors(x):
        if isinstance(x, torch.Tensor):
            return _unwrap_for_grad(x, level)
        assert False

    return tree_map(unwrap_tensors, inps)

def _is_differentiable(maybe_tensor):
    if not isinstance(maybe_tensor, torch.Tensor):
        return False
    return maybe_tensor.requires_grad

def _any_differentiable(tensor_or_tuple_of_tensors):
    flat_args, _ = tree_unflatten(tensor_or_tuple_of_tensors)
    return any(tuple(map(_is_differentiable, flat_args)))

def _wrap_tensor_for_grad(maybe_tensor, level):
    if not isinstance(maybe_tensor, torch.Tensor):
        return maybe_tensor
    return _wrap_for_grad(maybe_tensor, level)

def _wrap_all_tensors(tensor_or_tuple_of_tensors, level):
    return tree_map(partial(_wrap_tensor_for_grad, level=level), tensor_or_tuple_of_tensors)

# How do we increment and decrement the nesting? I don't think we can.
def vjp(f, *primals):
    level = _grad_increment_nesting()
    try:
        primals = _wrap_all_tensors(primals, level)
        diff_primals = _create_differentiable(primals, level)
        primals_out = f(*diff_primals)
        results = _undo_create_differentiable(primals_out, level)

        def wrapper(*cotangents, retain_graph=True, create_graph=True):
            result = torch.autograd.grad(primals_out, diff_primals, cotangents,
                                         retain_graph=retain_graph, create_graph=create_graph)
            return result

    finally:
        _grad_decrement_nesting()

    return results, wrapper

def jacrev(f):
    def wrapper_fn(primal):
        output, vjp_fn = vjp(f, primal)
        assert isinstance(output, torch.Tensor)
        # TODO: does jacrev compose with vmap...? the eye call should make it so that it doesn't
        basis = torch.eye(output.numel(), dtype=output.dtype, device=output.device) \
                     .view(output.numel(), *output.shape)
        result, = vmap(vjp_fn)(basis)
        result = result.view(*output.shape, *primal.shape)
        return result
    return wrapper_fn

def _safe_index(args, argnum):
    if not isinstance(argnum, int):
        raise RuntimeError(f'argnum must be int, got: {type(argnum)}')
    if argnum >= 0 and argnum < len(args):
        return args[argnum]
    raise RuntimeError(f'Got argnum={argnum}, but only {len(args)} inputs')

def _slice_argnums(args, argnums):
    if isinstance(argnums, int):
        return _safe_index(args, argnums)
    if isinstance(argnums, tuple):
        return tuple(_safe_index(args, i) for i in argnums)
    raise RuntimeError(f'argnums must be int or Tuple[int, ...], got: {type(argnums)}')

def grad_and_value(f, argnums=0, has_aux=False):
    def wrapper(*args):
        level = _grad_increment_nesting()
        output, aux, grad_input = None, None, None
        try:
            args = _wrap_all_tensors(args, level)
            diff_args = _slice_argnums(args, argnums)
            tree_map_(partial(_create_differentiable, level=level), diff_args)

            output = f(*args)
            if has_aux:
                output, aux = output

            if not isinstance(output, torch.Tensor):
                raise RuntimeError('grad_and_value(f)(*args): Expected f(*args)'
                                   f'to return a Tensor, got {type(output)}')
            if output.dim() != 0:
                raise RuntimeError('grad_and_value(f)(*args): Expected f(*args)'
                                   'to return a scalar Tensor, got tensor with '
                                   f'{output.dim()} dims. Maybe you wanted to'
                                   'use the vjp or jacrev APIs instead?')

            flat_diff_args, spec = tree_flatten(diff_args)

            # NB: need create_graph so that backward pass isn't run in no_grad mode
            flat_grad_input = torch.autograd.grad(
                output, flat_diff_args, create_graph=True, allow_unused=True)

            def replace_none_with_zeros(grad_arg, orig_arg):
                if grad_arg is None:
                    return torch.zeros_like(orig_arg)
                return grad_arg
            flat_grad_input = [replace_none_with_zeros(x, flat_diff_args[idx]) for idx, x in enumerate(flat_grad_input)]
            grad_input = tree_unflatten(flat_grad_input, spec)

        finally:
            if grad_input is not None:
                grad_input = _undo_create_differentiable(grad_input, level)
            if output is not None:
                output = _undo_create_differentiable(output, level)
            if aux is not None:
                aux = _undo_create_differentiable(aux, level)
            _grad_decrement_nesting()
        if has_aux:
            return grad_input, (output, aux)
        return grad_input, output
    return wrapper

def grad(f, argnums=0, has_aux=False):
    @wraps(f)
    def wrapper(*args):
        results = grad_and_value(f, argnums, has_aux=has_aux)(*args)
        if has_aux:
            grad, (value, aux) = results
            return grad, aux
        grad, value = results
        return grad
    return wrapper

