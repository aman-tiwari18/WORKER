"""
Detection models used by the unified detector — one wrapper per model file.

Each analytic reads the boxes of exactly one model (see unified_detector.model_for).
There is no fallback between models: an analytic whose model isn't loaded doesn't run.

  yolov10m                COCO, 80 classes      qp_box, furniture (and person analytics when
                                                UNIFIED_PERSON_MODEL=yolov10m)
  person_monitor_yolo26m  0 person, 1 monitor   person analytics (UNIFIED_PERSON_MODEL=finetuned),
                                                monitor_tamper (UNIFIED_MONITOR_MODEL=yolo26m)
  person_monitor_rfdetr   0 person, 1 monitor   monitor_tamper (UNIFIED_MONITOR_MODEL=rfdetr, default;
                                                monitor_alerts_deploy_v2/rfdetr_model/checkpoint_best_total.pth)
  mobile                  0 mobile              phone

Exam-safety rules every wrapper follows:
  * The unified worker loads the models its run needs before the first frame
    (load_now). A model that is not ready later reports unavailable and reloads on
    a background thread, so the frame loop never stalls on it.
  * A failed load (missing file, missing package, CUDA error) is logged once and
    retried after LOAD_RETRY_SECONDS, not on every batch.
  * One model instance is only ever used by one thread at a time (the unified
    worker runs each model's pass on its own thread, one batch at a time).
"""

import logging
import os
import threading
import time

import torch

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
LOAD_RETRY_SECONDS = float(os.environ.get("UNIFIED_MODEL_LOAD_RETRY_S", "300"))
GPU_BATCH_SIZE = int(os.environ.get("UNIFIED_GPU_BATCH_SIZE", "24"))


class _Backend:
    """Background-loaded model with a retry backoff. Subclasses implement _load / _predict."""

    def __init__(self, name: str, path: str):
        self.name = name
        self.path = path
        self._model = None
        self._loading = False
        self._failed_at = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        """True once loaded. Starts (or retries) the background load otherwise."""
        if self._model is not None:
            return True
        with self._lock:
            if self._model is None and not self._loading and (
                    self._failed_at is None or time.time() - self._failed_at >= LOAD_RETRY_SECONDS):
                self._loading = True
                threading.Thread(target=self._load_bg, name=f"load-{self.name}", daemon=True).start()
        return False

    def load_now(self) -> bool:
        """Blocking load, used by the unified worker at start for the models its run needs."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load_bg()
        return self._model is not None

    def _load_bg(self):
        try:
            if not os.path.exists(self.path):
                raise FileNotFoundError(self.path)
            t = time.time()
            model = self._load()
            self._model = model
            logger.info(f"[Models] {self.name} ready ({time.time() - t:.1f}s) <- {self.path}")
        except Exception as e:
            self._failed_at = time.time()
            logger.error(f"[Models] {self.name} unavailable, retry in {LOAD_RETRY_SECONDS:.0f}s: {e}")
        finally:
            self._loading = False

    def predict(self, frames):
        return self._predict(self._model, frames)

    def _load(self):
        raise NotImplementedError

    def _predict(self, model, frames):
        raise NotImplementedError


class YoloBackend(_Backend):
    def __init__(self, name, path, conf, imgsz=None):
        super().__init__(name, path)
        self.conf, self.imgsz = conf, imgsz

    def _load(self):
        from ultralytics import YOLO
        model = YOLO(self.path)
        if torch.cuda.is_available():
            model.to("cuda")
        return model

    def _predict(self, model, frames):
        kw = {"imgsz": self.imgsz} if self.imgsz else {}
        return model(frames, conf=self.conf, verbose=False, **kw)


class RFDetrBackend(_Backend):
    """RF-DETR Medium. Returns supervision.Detections (xyxy, confidence, class_id).
    On CUDA it is traced in FP16 for a fixed batch size, so smaller batches are
    padded with a copy of their last frame and the extra outputs dropped."""

    def __init__(self, name, path, conf, resolution=768, batch_size=GPU_BATCH_SIZE):
        super().__init__(name, path)
        self.conf, self.resolution, self.batch_size = conf, resolution, batch_size

    def _load(self):
        from rfdetr import RFDETRMedium
        model = RFDETRMedium(pretrain_weights=self.path, resolution=self.resolution, num_classes=2)
        if torch.cuda.is_available():
            optimize = getattr(model, "optimize_for_inference", None) or model.inference
            optimize(dtype=torch.float16, batch_size=self.batch_size)
        return model

    def _predict(self, model, frames):
        import cv2
        out = []
        for start in range(0, len(frames), self.batch_size):
            chunk = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames[start:start + self.batch_size]]
            padded = chunk + [chunk[-1]] * (self.batch_size - len(chunk))
            dets = model.predict(padded, threshold=self.conf, include_source_image=False)
            dets = dets if isinstance(dets, list) else [dets]
            out += dets[:len(chunk)]
        return out


def person_model_path(default_path):
    """Model file for the per-script person analytics (UNIFIED_DETECTOR=0 / standalone).
    Follows the same UNIFIED_PERSON_MODEL switch as the unified detector, so both paths
    count people with the same model. The fine-tuned model's person is class 0 like
    COCO's, and it predicts at its trained 960 px by default."""
    if os.environ.get("UNIFIED_PERSON_MODEL", "yolov10m").strip().lower() == "finetuned":
        return os.path.join(MODELS_DIR, "monitor_person_yolo26m.pt")
    return default_path


BACKENDS = {
    "yolov10m": YoloBackend("yolov10m", os.path.join(MODELS_DIR, "yolov10m.pt"), conf=0.45),
    # conf 0.35 = the lowest any consumer needs (monitor); person analytics get a >= 0.45 view.
    "person_monitor_yolo26m": YoloBackend("person_monitor_yolo26m",
                                          os.path.join(MODELS_DIR, "monitor_person_yolo26m.pt"), conf=0.35, imgsz=960),
    # conf 0.15: monitor_tamper's rules still apply their own 0.45 cut
    # (THRESHOLDS_RFDETR); the weaker boxes only feed its held-monitor fallback.
    "person_monitor_rfdetr": RFDetrBackend("person_monitor_rfdetr",
                                           os.path.join(MODELS_DIR, "monitor_person_rfdetr.pth"), conf=0.15),
    "mobile": YoloBackend("mobile", os.path.join(MODELS_DIR, "best_mobile_model.pt"), conf=0.25, imgsz=960),
}
