import argparse
import logging
import os
import sys
import tempfile
from itertools import chain
from typing import List

import onnx
import torch
from transformers import LlamaConfig, LlamaForCausalLM

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from benchmark_helper import Precision, prepare_environment, setup_logger  # noqa: E402
from llama_parity import main as parity_check  # noqa: E402
from onnx_model import OnnxModel  # noqa: E402

from onnxruntime import quantization  # noqa: E402

logger = logging.getLogger("")


def get_sample_inputs(config: LlamaConfig):
    batch_size, seq_len = 2, 8
    input_ids = torch.randint(low=0, high=config.vocab_size, size=(batch_size, seq_len), dtype=torch.int64)
    attn_mask = torch.ones(batch_size, seq_len, dtype=torch.int64)
    # pos_ids is of shape (batch_size, seq_len)
    pos_ids = attn_mask.long().cumsum(-1) - 1
    pos_ids.masked_fill_(attn_mask == 0, 1)

    return (input_ids, attn_mask, pos_ids)


def get_model_with_past_kv_inputs(config: LlamaConfig):
    batch_size, past_seq_len = 2, 8
    num_heads, head_size = config.num_attention_heads, int(config.hidden_size / config.num_attention_heads)
    input_ids = torch.randint(low=0, high=config.vocab_size, size=(batch_size, 1), dtype=torch.int64)
    attn_mask = torch.ones(batch_size, past_seq_len + 1, dtype=torch.int64)
    # pos_ids is of shape (batch_size, 1)
    pos_ids = attn_mask.long().cumsum(-1) - 1
    pos_ids.masked_fill_(attn_mask == 0, 1)
    pos_ids = pos_ids[:, -1].unsqueeze(-1)
    past_kv = [
        (
            torch.rand(batch_size, num_heads, past_seq_len, head_size),
            torch.rand(batch_size, num_heads, past_seq_len, head_size),
        )
        for _ in range(config.num_hidden_layers)
    ]

    return (input_ids, attn_mask, pos_ids, past_kv)


def get_model_dynamic_axes(input_names: List[str], output_names: List[str]):
    dynamic_axes = {}
    for name in input_names + output_names:
        if name in input_names:
            # shape is (batch_size, sequence_length)
            dynamic_axes[name] = {0: "batch_size", 1: "sequence_length"}
        elif name == "logits":
            # shape is (batch_size, sequence_length, vocab_size)
            dynamic_axes[name] = {0: "batch_size", 1: "sequence_length"}
        elif "present" in name:
            # shape is (batch_size, num_heads, past_sequence_length + 1, head_size)
            dynamic_axes[name] = {0: "batch_size", 2: "past_sequence_length + 1"}
        else:
            raise Exception("Unknown input or output name found")
    return dynamic_axes


def get_model_with_past_kv_dynamic_axes(input_names: List[str], output_names: List[str]):
    dynamic_axes = {}
    for name in input_names + output_names:
        if name in {"input_ids", "position_ids"}:
            # shape is (batch_size, 1)
            dynamic_axes[name] = {0: "batch_size"}
        elif name == "attention_mask":
            # shape is (batch_size, past_sequence_length + 1)
            dynamic_axes[name] = {0: "batch_size", 1: "past_sequence_length + 1"}
        elif "past" in name:
            # shape is (batch_size, num_heads, past_sequence_length, head_size)
            dynamic_axes[name] = {0: "batch_size", 2: "past_sequence_length"}
        elif name == "logits":
            # shape is (batch_size, 1, vocab_size)
            dynamic_axes[name] = {0: "batch_size"}
        elif "present" in name:
            # shape is (batch_size, num_heads, past_sequence_length + 1, head_size)
            dynamic_axes[name] = {0: "batch_size", 2: "past_sequence_length + 1"}
        else:
            raise Exception("Unknown input or output name found")
    return dynamic_axes


def save_onnx_model(onnx_model: onnx.ModelProto, output_path: str, data_path: str):
    onnx.save(
        onnx_model,
        output_path,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=data_path,
        size_threshold=1024,
        convert_attribute=False,
    )


# Notes:
# 1) Dynamo export will not work automatically until this issue is resolved: https://github.com/microsoft/onnxscript/issues/493
#
# 2) Dynamo export will run manually if you set the ONNX file path to the same path that you use to save the model after export.
# In other words, the value of `temp_path` should be set as the ONNX file path. You can open the issue in your browser to find
# the location in ONNX Script where you have to make this change.
#
# Once the issue is resolved, we can modify the code below as follows for each export.
#
# Before:
# temp_dir = args.output
# temp_path = os.path.join(temp_dir, "temp.onnx")
# ...
# ...
# ...
# del onnx_model
# os.system(f"rm {os.path.join(temp_dir, 'model.*')} && rm {os.path.join(temp_dir, '*.weight')} && rm {temp_path}")
#
#
# After:
# temp_dir = tempfile.TemporaryDirectory()
# temp_path = os.path.join(temp_dir.name, "temp.onnx")
# ...
# ...
# ...
# del onnx_model
# temp_dir.cleanup()
#
def run_dynamo_export(args: argparse.Namespace, l_config: LlamaConfig, llama: LlamaForCausalLM):
    from torch._dynamo import config

    config.capture_scalar_outputs = True

    # Export decoder_model.onnx
    input_ids, attn_mask, pos_ids = get_sample_inputs(l_config)
    temp_dir = args.output  # tempfile.TemporaryDirectory()
    temp_path = os.path.join(temp_dir, "temp.onnx")  # os.path.join(temp_dir.name, "temp.onnx")
    torch.onnx.dynamo_export(
        llama, input_ids, attn_mask, pos_ids, export_options=torch.onnx.ExportOptions(dynamic_shapes=True)
    ).save(temp_path)

    # Check decoder_model.onnx and save all external data to one file
    onnx.checker.check_model(temp_path)
    onnx.shape_inference.infer_shapes_path(temp_path)

    output_path = os.path.join(args.output, f"{args.model_name}_decoder_model_fp32.onnx")
    onnx_model = onnx.load_model(temp_path, load_external_data=True)
    save_onnx_model(onnx_model, output_path, f"{args.model_name}_decoder_model_fp32.onnx.data")
    del onnx_model
    os.system(
        f"rm {os.path.join(temp_dir, 'model.*')} && rm {os.path.join(temp_dir, '*.weight')} && rm {temp_path}"
    )  # temp_dir.cleanup()

    # Export decoder_with_past_model.onnx
    input_ids, attn_mask, pos_ids, past_kv = get_model_with_past_kv_inputs(l_config)
    temp_dir = args.output  # tempfile.TemporaryDirectory()
    temp_path = os.path.join(temp_dir, "temp.onnx")  # os.path.join(temp_dir.name, "temp.onnx")
    torch.onnx.dynamo_export(
        llama, input_ids, attn_mask, pos_ids, past_kv, export_options=torch.onnx.ExportOptions(dynamic_shapes=True)
    ).save(temp_path)

    # Check decoder_with_past_model.onnx and save all external data to one file
    onnx.checker.check_model(temp_path)
    onnx.shape_inference.infer_shapes_path(temp_path)

    output_path = os.path.join(args.output, f"{args.model_name}_decoder_with_past_model_fp32.onnx")
    onnx_model = onnx.load_model(temp_path, load_external_data=True)
    save_onnx_model(onnx_model, output_path, f"{args.model_name}_decoder_with_past_model_fp32.onnx.data")
    del onnx_model
    os.system(
        f"rm {os.path.join(temp_dir, 'model.*')} && rm {os.path.join(temp_dir, '*.weight')} && rm {temp_path}"
    )  # temp_dir.cleanup()

    logger.info(f"The {args.model_name} ONNX model has been successfully created with the Dynamo exporter!")


def run_torchscript_export(args: argparse.Namespace, l_config: LlamaConfig, llama: LlamaForCausalLM):
    # Export decoder_model.onnx
    decoder_inputs = get_sample_inputs(l_config)

    input_names = ["input_ids", "attention_mask", "position_ids"]
    output_names = [
        "logits",
        *list(
            chain.from_iterable((f"present.{i}.key", f"present.{i}.value") for i in range(l_config.num_hidden_layers))
        ),
    ]
    dynamic_axes = get_model_dynamic_axes(input_names, output_names)
    temp_dir = tempfile.TemporaryDirectory()
    temp_path = os.path.join(temp_dir.name, "temp.onnx")
    torch.onnx.export(
        llama,
        args=decoder_inputs,
        f=temp_path,
        export_params=True,
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=13,
        do_constant_folding=True,
        verbose=args.verbose,
    )

    # Check decoder_model.onnx and save all external data to one file
    onnx.checker.check_model(temp_path)
    onnx.shape_inference.infer_shapes_path(temp_path)

    output_path = os.path.join(args.output, f"{args.model_name}_decoder_model_fp32.onnx")
    onnx_model = onnx.load_model(temp_path, load_external_data=True)
    save_onnx_model(
        onnx_model,
        output_path,
        f"{args.model_name}_decoder_model_fp32.onnx.data",
    )
    del onnx_model
    temp_dir.cleanup()

    # Export decoder_with_past_model.onnx
    decoder_with_past_inputs = get_model_with_past_kv_inputs(l_config)
    input_names = [
        "input_ids",
        "attention_mask",
        "position_ids",
        *list(
            chain.from_iterable(
                (f"past_key_values.{i}.key", f"past_key_values.{i}.value") for i in range(l_config.num_hidden_layers)
            )
        ),
    ]
    output_names = [
        "logits",
        *list(
            chain.from_iterable((f"present.{i}.key", f"present.{i}.value") for i in range(l_config.num_hidden_layers))
        ),
    ]
    dynamic_axes = get_model_with_past_kv_dynamic_axes(input_names, output_names)
    temp_dir = tempfile.TemporaryDirectory()
    temp_path = os.path.join(temp_dir.name, "temp.onnx")
    torch.onnx.export(
        llama,
        args=decoder_with_past_inputs,
        f=temp_path,
        export_params=True,
        input_names=input_names,
        output_names=output_names,
        dynamic_axes=dynamic_axes,
        opset_version=13,
        do_constant_folding=True,
        verbose=args.verbose,
    )

    # Check decoder_with_past_model.onnx and save all external data to one file
    onnx.checker.check_model(temp_path)
    onnx.shape_inference.infer_shapes_path(temp_path)

    output_path = os.path.join(args.output, f"{args.model_name}_decoder_with_past_model_fp32.onnx")
    onnx_model = onnx.load_model(temp_path, load_external_data=True)
    save_onnx_model(
        onnx_model,
        output_path,
        f"{args.model_name}_decoder_with_past_model_fp32.onnx.data",
    )
    del onnx_model
    temp_dir.cleanup()

    logger.info(f"The {args.model_name} ONNX model has been successfully created with the TorchScript exporter!")


def remove_existing_files(output_path: str):
    for fle in os.listdir(output_path):
        filepath = os.path.join(output_path, fle)
        if ".onnx" in fle or ".onnx.data" in fle:
            os.remove(filepath)
            logger.warning(f"Removing {filepath}")


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-m",
        "--model_name",
        required=True,
        help="Model name in Hugging Face",
    )

    parser.add_argument(
        "-i",
        "--input",
        required=False,
        default=os.path.join("."),
        help="Directory path to PyTorch model and associated files if saved on disk",
    )

    parser.add_argument(
        "-o",
        "--output",
        required=False,
        default=os.path.join(".", "llama_onnx_models"),
        help="Directory path to save exported model files in",
    )

    parser.add_argument(
        "-p",
        "--precision",
        required=False,
        type=Precision,
        default=Precision.FLOAT32,
        choices=[Precision.FLOAT32, Precision.FLOAT16, Precision.INT8],
        help="Precision to export model in",
    )

    parser.add_argument(
        "-ep",
        "--execution_provider",
        required=False,
        default="cpu",
        choices=["cpu", "cuda", "rocm"],
        help="Execution provider to verify parity with",
    )

    parser.add_argument(
        "--quantize_embedding_layer",
        required=False,
        action="store_true",
        help="Quantize MatMul, GEMM, and Gather.",
    )
    parser.set_defaults(quantize_embedding_layer=False)

    parser.add_argument(
        "--quantize_per_channel",
        required=False,
        action="store_true",
        help="Quantize weights per each channel.",
    )
    parser.set_defaults(quantize_per_channel=False)

    parser.add_argument(
        "--quantize_reduce_range",
        required=False,
        action="store_true",
        help="Quantize weights with 7 bits.",
    )
    parser.set_defaults(quantize_reduce_range=False)

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print verbose logs",
    )
    parser.set_defaults(verbose=False)

    parser.add_argument(
        "-d",
        "--use_dynamo_export",
        action="store_true",
        help="Use the new Dynamo exporter instead of the old TorchScript exporter",
    )
    parser.set_defaults(use_dynamo_export=False)

    args = parser.parse_args()
    return args


def main():
    args = get_args()
    setup_logger(args.verbose)
    prepare_environment(args.input, args.output, args.execution_provider != "cpu")
    remove_existing_files(args.output)
    logger.info(f"Arguments: {args}")

    # Load model and config
    use_auth_token = args.input == os.path.join(".")
    l_config = LlamaConfig.from_pretrained(
        args.model_name if use_auth_token else args.input, use_auth_token=use_auth_token
    )
    llama = LlamaForCausalLM.from_pretrained(
        args.model_name if use_auth_token else args.input, use_auth_token=use_auth_token, use_cache=True
    )
    original_model_name = args.model_name
    args.model_name = args.model_name.split("/")[-1]

    # Export to ONNX
    if args.use_dynamo_export:
        logger.warning("Please ensure you have installed PyTorch, ONNX, and ONNX Script as follows.")
        logger.warning("Step 1 - PyTorch nightly: https://pytorch.org/get-started/locally/")
        logger.warning("Step 2 - ONNX weekly: https://pypi.org/project/onnx-weekly/")
        logger.warning(
            "Step 3 - ONNX Script from source: https://github.com/microsoft/onnxscript#installing-onnx-script"
        )
        logger.warning(
            "Note: When you install ONNX weekly, omit `onnx` when running the first line for installing ONNX Script. \
                        This is because you already installed `onnx-weekly` in the previous step."
        )
        run_dynamo_export(args, l_config, llama)
    else:
        run_torchscript_export(args, l_config, llama)

    # Change precision of exported models if not FP32
    decoder_model_fp32_path = os.path.join(args.output, f"{args.model_name}_decoder_model_fp32.onnx")
    decoder_with_past_model_fp32_path = os.path.join(
        args.output, f"{args.model_name}_decoder_with_past_model_fp32.onnx"
    )

    if args.precision == Precision.FLOAT16:
        # Convert decoder_model.onnx to FP16
        decoder_model_fp16_path = os.path.join(args.output, f"{args.model_name}_decoder_model_fp16.onnx")
        model = OnnxModel(onnx.load_model(decoder_model_fp32_path, load_external_data=True))
        model.convert_float_to_float16(keep_io_types=False, op_block_list=["If"])
        model.save_model_to_file(decoder_model_fp16_path, use_external_data_format=True, all_tensors_to_one_file=True)
        del model

        # Convert decoder_with_past_model.onnx to FP16
        decoder_with_past_model_fp16_path = os.path.join(
            args.output, f"{args.model_name}_decoder_with_past_model_fp16.onnx"
        )
        model = OnnxModel(onnx.load_model(decoder_with_past_model_fp32_path, load_external_data=True))
        model.convert_float_to_float16(keep_io_types=False, op_block_list=["If"])
        model.save_model_to_file(
            decoder_with_past_model_fp16_path, use_external_data_format=True, all_tensors_to_one_file=True
        )
        del model

    elif args.precision == Precision.INT8:
        # Convert decoder_model.onnx to INT8
        decoder_model_int8_path = os.path.join(args.output, f"{args.model_name}_decoder_model_int8.onnx")
        quantization.quantize_dynamic(
            decoder_model_fp32_path,
            decoder_model_int8_path,
            op_types_to_quantize=["MatMul", "Gemm", "Gather"] if args.quantize_embedding_layer else ["MatMul", "Gemm"],
            per_channel=args.quantize_per_channel,
            reduce_range=args.quantize_reduce_range,
            use_external_data_format=True,
            extra_options={"MatMulConstBOnly": True},
        )

        # Convert decoder_with_past_model.onnx to INT8
        decoder_with_past_model_int8_path = os.path.join(
            args.output, f"{args.model_name}_decoder_with_past_model_int8.onnx"
        )
        quantization.quantize_dynamic(
            decoder_with_past_model_fp32_path,
            decoder_with_past_model_int8_path,
            op_types_to_quantize=["MatMul", "Gemm", "Gather"] if args.quantize_embedding_layer else ["MatMul", "Gemm"],
            per_channel=args.quantize_per_channel,
            reduce_range=args.quantize_reduce_range,
            use_external_data_format=True,
            extra_options={"MatMulConstBOnly": True},
        )

    # Verify parity on all saved ONNX models
    del llama  # Delete LLaMA model from memory since it will be loaded again during parity check
    logger.info("Verifying parity on all ONNX models created")
    for fle in os.listdir(args.output):
        if ".data" in fle or ".onnx" not in fle:
            continue

        parity_cmd = ["-m", f"{original_model_name}", "-o", f"{os.path.join(args.output, fle)}"]
        if "with_past" in fle:
            parity_cmd.append("--use_past_kv")
        parity_check(parity_cmd)


if __name__ == "__main__":
    main()