"""Export a (fine-tuned) RF-DETR checkpoint to ONNX. Writes to the manifest's
`onnx` path and reports the output tensors NvDsInferParseRFDETR expects:
`dets` [B, Q, 4] (cx, cy, w, h in 0-1) and `labels` [B, Q, C] (logits).

    cd scripts/detection && python -m pathb.export.export_rfdetr --variant RFDETRMedium

Needs: rfdetr, onnx.

RF-DETR's exporter writes a FIXED batch size. Export with --batch equal to the
manifest's max_batch; nvinfer pads partial batches, so a fixed-batch engine works
(at the cost of always paying for a full batch).
"""
import argparse
import glob
import os
import shutil
import tempfile

import onnx
import rfdetr

from pathb.manifest import load_manifest


def shapes(path):
    m = onnx.load(path)
    shape = lambda v: [d.dim_param or d.dim_value for d in v.type.tensor_type.shape.dim]
    return {i.name: shape(i) for i in m.graph.input}, {o.name: shape(o) for o in m.graph.output}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="rfdetr")
    ap.add_argument("--variant", default="RFDETRMedium",
                    help="rfdetr class the checkpoint was trained with (RFDETRNano/Small/Medium/Base/Large)")
    ap.add_argument("--batch", type=int, default=None, help="default: manifest max_batch")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    spec = load_manifest().models[args.model]
    batch = args.batch or spec.max_batch
    model = getattr(rfdetr, args.variant)(pretrain_weights=spec.weights, resolution=spec.input_size)

    out_dir = tempfile.mkdtemp(prefix="rfdetr_export_")
    model.export(output_dir=out_dir, simplify=True, batch_size=batch, opset_version=args.opset)
    found = glob.glob(os.path.join(out_dir, "*.onnx"))
    if not found:
        raise SystemExit(f"no ONNX written to {out_dir}")
    onnx_path = max(found, key=os.path.getsize)

    inputs, outputs = shapes(onnx_path)
    print(f"inputs  {inputs}\noutputs {outputs}")
    if "dets" not in outputs or "labels" not in outputs:
        print("WARNING: outputs are not named dets/labels — the parser will fall back to shapes "
              "([Q,4] = boxes); update output-blob-names in configs/nvinfer_rfdetr.txt")
    classes = outputs.get("labels", [None])[-1]
    if isinstance(classes, int) and classes != spec.num_classes:
        print(f"NOTE: model has {classes} class logits; manifest num_classes={spec.num_classes} "
              "(set num_classes to the logits count, class_map to the classes you use)")

    os.makedirs(os.path.dirname(spec.onnx), exist_ok=True)
    shutil.move(onnx_path, spec.onnx)
    print(f"wrote {spec.onnx} (fixed batch {batch})")


if __name__ == "__main__":
    main()
