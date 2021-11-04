// Copyright (c) Facebook, Inc. and its affiliates.
// All rights reserved.
//
// This source code is licensed under the BSD-style license found in the
// LICENSE file in the root directory of this source tree.

#include <functorch/csrc/TensorWrapper.h>
#include <functorch/csrc/DynamicLayer.h>
#include <functorch/csrc/BatchedTensorImpl.h>

namespace at { namespace functorch {

Tensor makeBatched(const Tensor& tensor, int64_t level, optional<int64_t> bdim) {
  if (bdim.has_value()) {
    TORCH_INTERNAL_ASSERT(*bdim >= 0);
    TORCH_INTERNAL_ASSERT(*bdim < tensor.dim());
    return makeBatched(tensor, level, bdim.value());
  }
  return tensor;
}

std::vector<Tensor> makeBatchedVector(const std::vector<Tensor>& tensors, int64_t level, optional<int64_t> bdim) {
  std::vector<Tensor> res;
  for (size_t idx = 0; idx < tensors.size(); idx++) {
    res.push_back(makeBatched(tensors[idx], level, bdim));
  }
  return res;
}

std::tuple<Tensor, optional<int64_t>> unwrapTensorAtLevel(const Tensor& tensor, int64_t level) {
  auto* batched = maybeGetBatchedImpl(tensor);
  if (!batched) {
    return std::make_tuple(tensor, nullopt);
  }
  if (batched->level() == level) {
    return std::make_tuple(batched->value(), batched->bdim());
  }
  return std::make_tuple(tensor, nullopt);
}

}}
