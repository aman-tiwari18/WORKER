"""Export YOLO26 weights to ONNX for TensorRT: end-to-end (NMS-free) head,
dynamic batch, FP32 I/O. Writes to the manifest's `onnx` path.

    cd scripts/detection && python -m pathb.export.export_yolo26 [--model yolo26]

Needs: ultralytics, onnx, onnxslim.
"""
import argparse
import os
import shutil

import onnx
from ultralytics import YOLO

from pathb.manifest import load_manifest


def output_shapes(path):
    m = onnx.load(path)
    shape = lambda v: [d.dim_param or d.dim_value for d in v.type.tensor_type.shape.dim]
    return ({i.name: shape(i) for i in m.graph.input}, {o.name: shape(o) for o in m.graph.output})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="yolo26")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    spec = load_manifest().models[args.model]
    exported = YOLO(spec.weights).export(
        format="onnx", dynamic=True, imgsz=spec.input_size, opset=args.opset,
        simplify=True, half=False,
        nms=False)  # NMS-free head; newer Ultralytics (8.4.1xx) exports the raw head unless nms=False

    inputs, outputs = output_shapes(exported)
    print(f"inputs  {inputs}\noutputs {outputs}")
    out = outputs.get("output0")
    if not out or len(out) != 3 or out[-1] != 6:
        raise SystemExit(
            f"expected end-to-end output0 [batch, 300, 6], got {outputs}. "
            "NvDsInferParseYolo26 needs the NMS-free YOLO26 head.")
    if isinstance(next(iter(inputs.values()))[0], int):
        raise SystemExit("batch dimension is static — re-export with dynamic=True")

    os.makedirs(os.path.dirname(spec.onnx), exist_ok=True)
    shutil.move(exported, spec.onnx)
    print(f"wrote {spec.onnx}")


if __name__ == "__main__":
    main()
