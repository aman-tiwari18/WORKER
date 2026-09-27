"""Accuracy gate: compare the exported YOLO26 model against the PyTorch model on
real footage before switching to Plan B.

    cd scripts/detection && python -m pathb.export.validate_engine \
        --source /path/to/video.mp4 [--source /dir/of/images] [--every 25] [--max-frames 500]

Runs every sampled frame through
  - the PyTorch model (Ultralytics, the current production path), and
  - the ONNX export via ONNX Runtime (or the TensorRT engine with --trt),
decoded exactly like nvinfer + NvDsInferParseYolo26 (pathb/decode_ref.py), and
reports how many PyTorch boxes the export reproduces (same class, IoU >= 0.5).

Pass criteria are yours to set; a reasonable start is recall and precision >= 0.97
at the production threshold. FP16 should land very close to FP32.
"""
import argparse
import glob
import os

import cv2
import numpy as np

from pathb.decode_ref import decode_yolo26, letterbox, match
from pathb.manifest import load_manifest

IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp")


def frames(sources, every, max_frames):
    n = 0
    for src in sources:
        if os.path.isdir(src):
            for p in sorted(glob.glob(os.path.join(src, "**", "*"), recursive=True)):
                if p.lower().endswith(IMG_EXT):
                    img = cv2.imread(p)
                    if img is not None:
                        yield p, img
                        n += 1
                        if n >= max_frames:
                            return
            continue
        cap = cv2.VideoCapture(src)
        i = 0
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if i % every == 0:
                yield f"{src}#{i}", img
                n += 1
                if n >= max_frames:
                    cap.release()
                    return
            i += 1
        cap.release()


class OnnxRunner:
    def __init__(self, path):
        import onnxruntime as ort
        prov = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
        self.sess = ort.InferenceSession(path, providers=prov)
        self.inp = self.sess.get_inputs()[0].name

    def __call__(self, x):
        return self.sess.run(None, {self.inp: x})[0][0]


class TrtRunner:
    def __init__(self, path):
        import tensorrt as trt
        import torch
        self.torch = torch
        logger = trt.Logger(trt.Logger.WARNING)
        with open(path, "rb") as f:
            self.engine = trt.Runtime(logger).deserialize_cuda_engine(f.read())
        self.ctx = self.engine.create_execution_context()
        self.names = [self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)]
        self.inp = next(n for n in self.names if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT)
        self.out = next(n for n in self.names if n != self.inp)

    def __call__(self, x):
        t = self.torch.from_numpy(x).cuda()
        self.ctx.set_input_shape(self.inp, tuple(t.shape))
        out = self.torch.empty(tuple(self.ctx.get_tensor_shape(self.out)), dtype=self.torch.float32, device="cuda")
        self.ctx.set_tensor_address(self.inp, t.data_ptr())
        self.ctx.set_tensor_address(self.out, out.data_ptr())
        self.ctx.execute_async_v3(self.torch.cuda.current_stream().cuda_stream)
        self.torch.cuda.synchronize()
        return out[0].cpu().numpy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="yolo26")
    ap.add_argument("--source", action="append", required=True, help="video file or image folder (repeatable)")
    ap.add_argument("--every", type=int, default=25, help="video: take every Nth frame")
    ap.add_argument("--max-frames", type=int, default=500)
    ap.add_argument("--trt", action="store_true", help="test the TensorRT engine instead of the ONNX file")
    ap.add_argument("--conf", type=float, default=None, help="default: manifest conf_threshold")
    args = ap.parse_args()

    from ultralytics import YOLO
    spec = load_manifest().models[args.model]
    thr = args.conf if args.conf is not None else spec.conf_threshold
    ref_model = YOLO(spec.weights)
    runner = TrtRunner(spec.engine) if args.trt else OnnxRunner(spec.onnx)

    tot_m = tot_ref = tot_test = n = 0
    worst = []
    for name, img in frames(args.source, args.every, args.max_frames):
        r = ref_model(img, conf=thr, imgsz=spec.input_size, verbose=False)[0]
        ref = [(int(b.cls[0]), float(b.conf[0]), *map(float, b.xyxy[0].tolist())) for b in r.boxes]
        x, scale, pad = letterbox(img, spec.input_size)
        test = decode_yolo26(runner(x), thr, scale, pad, img.shape)
        m, nr, nt = match(ref, test)
        tot_m, tot_ref, tot_test, n = tot_m + m, tot_ref + nr, tot_test + nt, n + 1
        if nr and m / nr < 0.9:
            worst.append((m / nr, name, nr, nt))

    recall = tot_m / tot_ref if tot_ref else 1.0
    precision = tot_m / tot_test if tot_test else 1.0
    print(f"frames {n} | PyTorch boxes {tot_ref} | export boxes {tot_test} | matched {tot_m}")
    print(f"recall {recall:.4f}  precision {precision:.4f}  ({'TensorRT' if args.trt else 'ONNX'} vs PyTorch, conf {thr})")
    for frac, name, nr, nt in sorted(worst)[:10]:
        print(f"  low agreement {frac:.2f}: {name} (ref {nr}, export {nt})")


if __name__ == "__main__":
    main()
