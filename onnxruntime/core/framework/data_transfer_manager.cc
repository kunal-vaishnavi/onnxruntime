// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#include "core/framework/data_transfer_manager.h"
#include "core/framework/tensor.h"
#include "core/framework/sparse_tensor.h"

namespace onnxruntime {
using namespace common;

Status DataTransferManager::RegisterDataTransfer(std::unique_ptr<IDataTransfer> data_transfer) {
  if (nullptr == data_transfer) {
    return Status(ONNXRUNTIME, INVALID_ARGUMENT, "data_transfer registered is nullptr.");
  }
  datatransfers_.push_back(std::move(data_transfer));
  return Status::OK();
}

Status DataTransferManager::UnregisterDataTransfer(IDataTransfer* data_transfer) {
  auto iter = std::find_if(datatransfers_.begin(), datatransfers_.end(),
                           [&data_transfer](const std::unique_ptr<IDataTransfer>& dt) {
                             return dt.get() == data_transfer;
                           });

  if (iter != datatransfers_.end()) {
    datatransfers_.erase(iter);
  }

  // ignore if not found
  return Status::OK();
}

const IDataTransfer* DataTransferManager::GetDataTransfer(const OrtDevice& src_device, const OrtDevice& dst_device) const {
  for (auto& data_transfer : datatransfers_) {
    if (!data_transfer->CanCopy(src_device, dst_device)) {
      continue;
    }

    return data_transfer.get();
  }
  return nullptr;
}

Status DataTransferManager::CopyTensor(const Tensor& src, Tensor& dst) const {
  if (src.Shape().Size() != dst.Shape().Size()) {
    return Status(ONNXRUNTIME, FAIL, "Tensor size mismatch");
  }

  for (auto& data_transfer : datatransfers_) {
    if (!data_transfer->CanCopy(src.Location().device, dst.Location().device)) {
      continue;
    }

    return data_transfer->CopyTensor(src, dst);
  }

  return ORT_MAKE_STATUS(ONNXRUNTIME,
                         FAIL,
                         "There's no data transfer registered for copying tensors from ",
                         src.Location().device.ToString(),
                         " to ",
                         dst.Location().device.ToString());
}

Status DataTransferManager::CopyTensorAsync(const Tensor& src, Tensor& dst, Stream& stream) const {
  if (src.Shape().Size() != dst.Shape().Size()) {
    return Status(ONNXRUNTIME, FAIL, "Tensor size mismatch");
  }

  for (auto& data_transfer : datatransfers_) {
    if (!data_transfer->CanCopy(src.Location().device, dst.Location().device)) {
      continue;
    }

    return data_transfer->CopyTensorAsync(src, dst, stream);
  }

  return ORT_MAKE_STATUS(ONNXRUNTIME,
                         FAIL,
                         "There's no data transfer registered for copying tensors from ",
                         src.Location().device.ToString(),
                         " to ",
                         dst.Location().device.ToString());
}

#if !defined(DISABLE_SPARSE_TENSORS)
Status DataTransferManager::CopySparseTensor(const SparseTensor& src, SparseTensor& dst) const {
  if (src.DenseShape().Size() != dst.DenseShape().Size()) {
    return Status(ONNXRUNTIME, FAIL, "Tensor size mismatch");
  }

  for (auto& data_transfer : datatransfers_) {
    if (!data_transfer->CanCopy(src.Location().device, dst.Location().device)) {
      continue;
    }

    return src.Copy(*data_transfer, dst);
  }

  return ORT_MAKE_STATUS(ONNXRUNTIME,
                         FAIL,
                         "There's no data transfer registered for copying tensors from ",
                         src.Location().device.ToString(),
                         " to ",
                         dst.Location().device.ToString());
}
#endif

common::Status DataTransferManager::CopyTensors(const std::vector<IDataTransfer::SrcDstPair>& src_dst_pairs) const {
  if (src_dst_pairs.empty())
    return Status::OK();

  const auto& first_pair = src_dst_pairs.front();
  const OrtDevice& src_device = first_pair.src.get().Location().device;
  const OrtDevice& dst_device = first_pair.dst.get().Location().device;

  bool all_same = std::all_of(src_dst_pairs.cbegin() + 1, src_dst_pairs.cend(),
                              [&src_device, &dst_device](const IDataTransfer::SrcDstPair& pair) {
                                return pair.src.get().Location().device == src_device &&
                                       pair.dst.get().Location().device == dst_device;
                              });

  IDataTransfer* first_dt = nullptr;

  for (auto& data_transfer : datatransfers_) {
    if (data_transfer->CanCopy(src_device, dst_device)) {
      first_dt = data_transfer.get();
      break;
    }
  }

  if (first_dt == nullptr) {
    return ORT_MAKE_STATUS(ONNXRUNTIME,
                           FAIL,
                           "There's no data transfer registered for copying tensors from ",
                           src_device.ToString(),
                           " to ",
                           dst_device.ToString());
  }

  // all copies are between the same devices so we can do them all at once
  if (all_same) {
    return first_dt->CopyTensors(src_dst_pairs);
  }

  // there are a mix of devices requiring copies. we don't expect this to happen, so just iterate the pairs
  // copying one at a time. if this becomes expected we could create a list for each IDataTransfer instance so we
  // batch as much as possible.

  // copy the first one as we already did the IDataTransfer lookup
  ORT_RETURN_IF_ERROR(first_pair.src_stream ? first_dt->CopyTensorAsync(first_pair.src.get(), first_pair.dst.get(), *(first_pair.src_stream))
                                            : first_dt->CopyTensor(first_pair.src.get(), first_pair.dst.get()));

  for (auto cur_pair = src_dst_pairs.cbegin() + 1, end_pair = src_dst_pairs.cend(); cur_pair != end_pair; ++cur_pair) {
    ORT_RETURN_IF_ERROR(!cur_pair->src_stream ? CopyTensor(cur_pair->src, cur_pair->dst) : CopyTensorAsync(cur_pair->src, cur_pair->dst, *(cur_pair->src_stream)));
  }

  return Status::OK();
}

#if !defined(DISABLE_SPARSE_TENSORS)
common::Status DataTransferManager::CopySparseTensors(const std::vector<IDataTransfer::SparseSrcDstPair>& src_dst_pairs) const {
  if (src_dst_pairs.empty())
    return Status::OK();

  const auto& first_pair = src_dst_pairs.front();
  const OrtDevice& src_device = first_pair.src.get().Location().device;
  const OrtDevice& dst_device = first_pair.dst.get().Location().device;

  bool all_same = std::all_of(src_dst_pairs.cbegin() + 1, src_dst_pairs.cend(),
                              [&src_device, &dst_device](const IDataTransfer::SparseSrcDstPair& pair) {
                                return pair.src.get().Location().device == src_device &&
                                       pair.dst.get().Location().device == dst_device;
                              });

  IDataTransfer* first_dt = nullptr;

  for (auto& data_transfer : datatransfers_) {
    if (data_transfer->CanCopy(src_device, dst_device)) {
      first_dt = data_transfer.get();
      break;
    }
  }

  if (first_dt == nullptr) {
    return ORT_MAKE_STATUS(ONNXRUNTIME,
                           FAIL,
                           "There's no data transfer registered for copying tensors from ",
                           src_device.ToString(),
                           " to ",
                           dst_device.ToString());
  }

  // all copies are between the same devices so we can do them all at once
  if (all_same) {
    return first_dt->CopySparseTensors(src_dst_pairs);
  }

  // there are a mix of devices requiring copies. we don't expect this to happen, so just iterate the pairs
  // copying one at a time. if this becomes expected we could create a list for each IDataTransfer instance so we
  // batch as much as possible.

  // copy the first one as we already did the IDataTransfer lookup
  ORT_RETURN_IF_ERROR(first_pair.src.get().Copy(*first_dt, first_pair.dst));

  for (auto cur_pair = src_dst_pairs.cbegin() + 1, end_pair = src_dst_pairs.cend(); cur_pair != end_pair; ++cur_pair) {
    ORT_RETURN_IF_ERROR(CopySparseTensor(cur_pair->src, cur_pair->dst));
  }

  return Status::OK();
}
#endif

}  // namespace onnxruntime
