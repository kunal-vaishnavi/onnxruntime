// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#ifdef _WIN32
#pragma warning(disable : 4244)
#endif

#include "mixed_precision_scale_impl.h"
#include <cuda_fp16.h>
#include "core/providers/cuda/cu_inc/common.cuh"

namespace onnxruntime {
namespace cuda {

template <typename SrcT, typename DstT>
__global__ void _MixedPrecisionScale(
    const SrcT* input_data,
    const float* scale_data,
    DstT* output_data,
    CUDA_LONG N) {
  CALCULATE_ELEMENTWISE_INDEX_OR_EXIT(id, N);
  output_data[id] = static_cast<DstT>(*scale_data * static_cast<float>(input_data[id]));
}

template <typename SrcT, typename DstT>
void Impl_MixedPrecisionScale(
    cudaStream_t stream,
    const SrcT* input_data,
    const float* scale_data,
    DstT* output_data,
    size_t count) {
  int blocksPerGrid = static_cast<int>(CeilDiv(count, GridDim::maxThreadsPerBlock));
  CUDA_LONG N = static_cast<CUDA_LONG>(count);
  _MixedPrecisionScale<SrcT, DstT><<<blocksPerGrid, GridDim::maxThreadsPerBlock, 0, stream>>>(
      input_data,
      scale_data,
      output_data,
      N);
}

#define SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(SrcT, DstT) \
  template void Impl_MixedPrecisionScale<SrcT, DstT>(   \
      cudaStream_t stream,                              \
      const SrcT* input_data,                           \
      const float* scale_data,                          \
      DstT* output_data,                                \
      size_t count);

SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(half, half)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(half, float)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(float, half)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(float, float)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(BFloat16, BFloat16)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(BFloat16, float)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(float, BFloat16)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(BFloat16, half)
SPECIALIZE_MIXEDPRECISIONSCALE_IMPL(half, BFloat16)

}  // namespace cuda
}  // namespace onnxruntime
