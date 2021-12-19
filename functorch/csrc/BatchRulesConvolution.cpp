// Copyright (c) Facebook, Inc. and its affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include <functorch/csrc/BatchRulesHelper.h>
#include <functorch/csrc/PlumbingHelper.h>
#include <ATen/core/dispatch/Dispatcher.h>

namespace at { namespace functorch {

// convolution_batch_rule translated from jax with modifications:
// https://github.com/google/jax/blob/master/jax/_src/lax/lax.py#L3143

// PyTorch's convolution is different from JAX's conv_general_dilated:
// we do not support batch_group_count (which is needed for convolution backwards).
// Instead, there's a convolution_backward op that needs a batching rule.
std::tuple<Tensor,optional<int64_t>>
convolution_batch_rule(const Tensor& lhs, optional<int64_t> lhs_bdim, const Tensor& rhs, optional<int64_t> rhs_bdim, const optional<Tensor>& bias, optional<int64_t> bias_bdim, IntArrayRef stride, IntArrayRef padding, IntArrayRef dilation, bool transposed, IntArrayRef output_padding, int64_t groups) {
  DimVector lhs_spec(stride.size() + 2);
  std::iota(lhs_spec.begin(), lhs_spec.end(), 0);
  DimVector rhs_spec = lhs_spec;
  DimVector out_spec = lhs_spec;
  if (transposed) {
    rhs_spec[0] = 1;
    rhs_spec[1] = 0;
  }

  // If we have a batched bias or weight, we need to perform the computation separately.
  optional<Tensor> unbatched_bias;
  bool separate_bias;
  if ((rhs_bdim && bias && bias->defined()) || bias_bdim) {
    TORCH_INTERNAL_ASSERT(bias.has_value());
    TORCH_INTERNAL_ASSERT(bias->defined());
    unbatched_bias = nullopt;
    separate_bias = true;
  } else {
    unbatched_bias = bias;
    separate_bias = false;
  }
  std::tuple<Tensor, optional<int64_t>> result;
  if (lhs_bdim && !rhs_bdim) {
    auto new_x = reshape_dim_into(*lhs_bdim, lhs_spec[0], lhs);
    auto out = at::convolution(new_x, rhs, unbatched_bias, stride, padding, dilation, transposed, output_padding, groups);
    out = reshape_dim_outof(out_spec[0], lhs.sizes()[*lhs_bdim], out);
    result = std::make_tuple(out, out_spec[0]);
  } else if (!lhs_bdim && rhs_bdim) {
    if (groups == 1) {
      auto new_w = reshape_dim_into(*rhs_bdim, rhs_spec[0], rhs);
      auto out = at::convolution(lhs, new_w, unbatched_bias, stride, padding, dilation, transposed, output_padding, groups);
      out = reshape_dim_outof(out_spec[1], rhs.sizes()[*rhs_bdim], out);
      result = std::make_tuple(out, out_spec[1]);
    } else {
      auto dim_with_groups = transposed ? 1 : 0;
      auto new_w = reshape_dim_outof(rhs_spec[dim_with_groups] + (*rhs_bdim <= rhs_spec[0]), groups, rhs);
      new_w = reshape_dim_into(*rhs_bdim + (rhs_spec[0] < rhs_bdim), rhs_spec[0] + 1, new_w);
      new_w = reshape_dim_into(rhs_spec[0], rhs_spec[0], new_w);
      auto out = at::convolution(lhs, new_w, unbatched_bias, stride, padding, dilation, transposed, output_padding, groups);
      out = reshape_dim_outof(out_spec[1], groups, out);
      out = reshape_dim_outof(out_spec[1] + 1, rhs.sizes()[*rhs_bdim], out);
      out = reshape_dim_into(out_spec[1], out_spec[1] + 1, out);
      result = std::make_tuple(out, out_spec[1]);
    }
  } else if (lhs_bdim && rhs_bdim) {
    auto new_x = reshape_dim_into(*lhs_bdim, lhs_spec[1], lhs);
    groups *= lhs.sizes()[*lhs_bdim];
    auto dim_with_groups = transposed ? 1 : 0;
    auto new_w = reshape_dim_into(*rhs_bdim, rhs_spec[dim_with_groups], rhs);
    auto out = at::convolution(new_x, new_w, unbatched_bias, stride, padding, dilation, transposed, output_padding, groups);
    out = reshape_dim_outof(out_spec[1], lhs.sizes()[*lhs_bdim], out);
    result = std::make_tuple(out, out_spec[1]);
  } else {
    result = std::make_tuple(at::convolution(lhs, rhs, unbatched_bias, stride, padding, dilation, transposed, output_padding, groups), nullopt);
  }
  if (separate_bias) {
    auto A = std::get<0>(result);
    auto A_batch_dim = std::get<1>(result);
    auto B = *bias;
    auto B_batch_dim = bias_bdim;
    A = moveBatchDimToFront(A, A_batch_dim);
    B = moveBatchDimToFront(B, B_batch_dim);
    for (size_t i = 0; i < out_spec.size() - 2; i++) {
      B = B.unsqueeze(-1);
    }
    B = maybePadToLogicalRank(B, B_batch_dim, rankWithoutBatchDim(A, A_batch_dim));

    return std::make_tuple(at::add(A, B), 0);
  } else {
    return result;
  }
}

Tensor _convolution_decomp(
    const Tensor& input_r, const Tensor& weight_r, const c10::optional<Tensor>& bias_r_opt,
    IntArrayRef stride_, IntArrayRef padding_, IntArrayRef dilation_,
    bool transposed_, IntArrayRef output_padding_, int64_t groups_,
    bool benchmark, bool deterministic, bool cudnn_enabled, bool allow_tf32) {
  // Ignore everything. If the user called this in the normal way,
  // then they should be fine.
  (void) benchmark;
  (void) deterministic;
  (void) cudnn_enabled;
  (void) allow_tf32;
  return at::convolution(
      input_r, weight_r, bias_r_opt, stride_, padding_, dilation_, transposed_, output_padding_, groups_);
}

// TODO: delete the following after confirming performance
// bool first_dim_has_size_1(const Tensor& value, int64_t bdim) {
//   if (bdim == 0) {
//     return value.size(1) == 1;
//   }
//   return value.size(0) == 1;
// }
//
// std::tuple<Tensor,int64_t,Tensor,int64_t> cudnn_conv_per_sample_grad_rule(
//     const Tensor& self, optional<int64_t> self_bdim,
//     const Tensor& grad_output, optional<int64_t> grad_output_bdim,
//     const Tensor& weight, optional<int64_t> weight_bdim,
//     IntArrayRef padding, IntArrayRef stride, IntArrayRef dilation, int64_t groups, bool benchmark,
//     bool deterministic, bool allow_tf32, std::array<bool, 2> output_mask) {
//   TORCH_INTERNAL_ASSERT(self_bdim && grad_output_bdim && !weight_bdim);
//   // TODO: No clue if this works if the first non-batch dim isn't size 1
//   TORCH_INTERNAL_ASSERT(first_dim_has_size_1(self, *self_bdim));
//   TORCH_INTERNAL_ASSERT(self.dim() == 5);
//
//   auto bdim_size = self.size(*self_bdim);
//   auto self_ = reshape_dim_into(*self_bdim, 0, self);
//   auto in_channels = self_.size(1);
//   auto grad_output_ = reshape_dim_into(*grad_output_bdim, 0, grad_output);
//
//   auto grad_self = at::cudnn_convolution_backward_input(
//       self_.sizes(), grad_output_, weight,
//       padding, stride, dilation, groups, benchmark, deterministic, allow_tf32);
//   grad_self = reshape_dim_outof(0, bdim_size, grad_self);
//
//   // Copied from https://github.com/pytorch/opacus/blob/master/opacus/grad_sample/conv.py
//   auto A = at::im2col(self_, {weight.size(2), weight.size(3)}, dilation, padding, stride);
//   auto B = grad_output_.reshape({bdim_size, -1, A.size(-1)});
//   auto grad_sample = at::einsum("noq,npq->nop", {B, A});
//   grad_sample = grad_sample.view({
//       bdim_size, groups, -1, groups, in_channels / groups,
//       weight.size(2) * weight.size(3) });
//   grad_sample = at::einsum("ngrg...->ngr...", {grad_sample});
//   grad_sample = grad_sample.reshape(
//       {bdim_size, weight.size(0), weight.size(1), weight.size(2), weight.size(3)});
//
//   return std::make_tuple(grad_self, 0, grad_sample, 0);
// }
//
// std::tuple<Tensor,Tensor> cudnn_convolution_backward_plumbing(const Tensor & self, const Tensor & grad_output, const Tensor & weight, IntArrayRef padding, IntArrayRef stride, IntArrayRef dilation, int64_t groups, bool benchmark, bool deterministic, bool allow_tf32, std::array<bool, 2> output_mask) {
//   auto maybe_layer = maybeCurrentDynamicLayer();
//   TORCH_INTERNAL_ASSERT(maybe_layer.has_value());
//   int64_t cur_level = maybe_layer->layerId();
//
//   Tensor self_value;
//   optional<int64_t> self_bdim;
//   std::tie(self_value, self_bdim) = unwrapTensorAtLevel(self, cur_level);
//   Tensor grad_output_value;
//   optional<int64_t> grad_output_bdim;
//   std::tie(grad_output_value, grad_output_bdim) = unwrapTensorAtLevel(grad_output, cur_level);
//   Tensor weight_value;
//   optional<int64_t> weight_bdim;
//   std::tie(weight_value, weight_bdim) = unwrapTensorAtLevel(weight, cur_level);
//
//   if (self_bdim.has_value() && self_value.dim() == 5 && first_dim_has_size_1(self_value, *self_bdim) && grad_output_bdim.has_value() && !weight_bdim.has_value()) {
//     c10::impl::ExcludeDispatchKeyGuard guard(kBatchedKey);
//     auto result = cudnn_conv_per_sample_grad_rule(
//         self_value, self_bdim,
//         grad_output_value, grad_output_bdim,
//         weight_value, weight_bdim,
//         padding, stride, dilation, groups,
//         benchmark, deterministic, allow_tf32, output_mask);
//     return std::make_tuple(
//         makeBatched(std::get<0>(result), std::get<1>(result), cur_level),
//         makeBatched(std::get<2>(result), std::get<3>(result), cur_level));
//   }
//
//   static auto op = c10::Dispatcher::singleton()
//     .findSchemaOrThrow("aten::cudnn_convolution_backward", "");
//   return slow_fallback<Tensor,Tensor>(op, { self, grad_output, weight, padding, stride, dilation, groups, benchmark, deterministic, allow_tf32, output_mask });
// }

TORCH_LIBRARY_IMPL(aten, FT_BATCHED_KEY, m) {
  VMAP_SUPPORT("convolution", convolution_batch_rule);
  m.impl("_convolution", _convolution_decomp);
}

}} // namespace at;:functorch
