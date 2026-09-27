"""Build TensorRT engines from the exported ONNX files with trtexec.

    cd scripts/detection && python -m pathb.export.build_engines [--model yolo26 ...]

Run on the SAME GPU type and TensorRT version as production (engines are not
portable). Precision comes from the manifest:
  fp16 -> --fp16
  fp8  -> --fp8 --stronglyTyped, and the ONNX must already carry FP8 Q/DQ nodes
          (quantise with NVIDIA TensorRT Model Optimizer first). Never INT8 for RF-DETR.
I/O stays FP32 (the parsers read FP32 outputs).
"""
import argparse
import os
import shutil
import subprocess

import onnx

from pathb.manifest import load_manifest


def input_info(path):
    inp = onnx.load(path).graph.input[0]
    dims = [d.dim_param or d.dim_value for d in inp.type.tensor_type.shape.dim]
    return inp.name, dims


def build(spec, workspace_mb):
    if not os.path.exists(spec.onnx):
        raise SystemExit(f"{spec.name}: {spec.onnx} missing — run the export script first")
    name, dims = input_info(spec.onnx)
    s = spec.input_size
    cmd = [shutil.which("trtexec") or "/usr/src/tensorrt/bin/trtexec",
           f"--onnx={spec.onnx}", f"--saveEngine={spec.engine}",
           f"--memPoolSize=workspace:{workspace_mb}"]
    if isinstance(dims[0], int):
        print(f"{spec.name}: static batch {dims[0]} — engine batch fixed at {dims[0]}")
    else:
        opt = max(1, spec.max_batch // 2)
        cmd += [f"--minShapes={name}:1x3x{s}x{s}",
                f"--optShapes={name}:{opt}x3x{s}x{s}",
                f"--maxShapes={name}:{spec.max_batch}x3x{s}x{s}"]
    if spec.precision == "fp16":
        cmd.append("--fp16")
    elif spec.precision == "fp8":
        cmd += ["--fp8", "--stronglyTyped"]
    elif spec.precision != "fp32":
        raise SystemExit(f"{spec.name}: unsupported precision {spec.precision}")
    os.makedirs(os.path.dirname(spec.engine), exist_ok=True)
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"{spec.name}: wrote {spec.engine}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", help="default: every enabled model")
    ap.add_argument("--workspace-mb", type=int, default=4096)
    args = ap.parse_args()
    manifest = load_manifest()
    names = args.model or [n for n, s in manifest.models.items() if s.enabled]
    for n in names:
        build(manifest.models[n], args.workspace_mb)


if __name__ == "__main__":
    main()
