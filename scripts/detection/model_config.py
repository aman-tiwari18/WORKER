"""
model_config.py — single source of truth for which YOLO checkpoint every
detection script loads.

THREE DETECTION MODELS (fine-tuned models, Sep 2026)
----------------------------------------------------
  PERSON_MODEL_PATH     monitor_person_model.pt  classes 0=person, 1=monitor
                        fine-tuned YOLO26m @960 on exam-lab CCTV. Used by every
                        person-based script: crowd, crowd_zero, crowd_five,
                        proximity, intruder, entry_exit, server, strongroom,
                        invigilator, blind_spot, crowd_biometric.
                        person keeps COCO id 0, so `cls == 0` checks still hold.
  PHONE_MODEL_PATH      phone_model.pt           class 0=mobile
                        fine-tuned YOLO26m @960. Used by phone_det_complete.
  DETECTION_MODEL_PATH  yolo26m.pt               stock COCO-80
                        still needed by the scripts that use classes the
                        fine-tuned models do not have: qp_box (boxes, bags...)
                        and furniture (chair / table / bench).

Override via .env: YOLO_PERSON_MODEL, YOLO_PHONE_MODEL, YOLO_DETECTION_MODEL.
The fine-tuned files are committed in models_bundled/ and pinned (sha256) in
models.lock.json under "bundled": the image build copies them into /opt/models and
docker-entrypoint.sh seeds the bind-mounted ./models from there, like the stock
models, so every image/host has them. If one is still missing (e.g. a host that
runs the code without the image), the script logs an error and falls back to the
stock COCO model (person is id 0 there too and the phone script also accepts
"cell phone"), so detection keeps running instead of crash-looping.
The input size (960 for the fine-tuned models, 640 for yolo26m) is read by
Ultralytics from each checkpoint, so callers do not pass imgsz.

Every core detection module (blind_spot_det.py, close_proximity.py,
Crowd_det_*.py, Entry_exit_det.py, improper_furniture_det.py, Intruder_det.py,
invigilator_tracking.py, phone_det_complete.py, qp_box_det.py,
Server_room_det.py, StrongRoom_det.py, unified_detector.py) used to hardcode
its own `MODEL_PATH = os.path.join(BASE_DIR, "models", "<file>.pt")`, so
swapping the model meant editing 15 files by hand and risking one getting
missed. They now all import DETECTION_MODEL_PATH from here instead.

Override via .env (loaded into the container/agent env already; these scripts
inherit it as regular subprocess env vars — no dotenv call needed here):
  YOLO_DETECTION_MODEL=yolo26m.pt   # general-purpose detector (person/chair/
                                     # table/phone/... COCO-80), used by the
                                     # 15 scripts above
  YOLO_INVERSION_MODEL=yolo26n.pt   # lightweight detector used only by
                                     # camera_inversion_worker's fast
                                     # person-presence pre-check

Both must be stock Ultralytics COCO-80 checkpoints (or fine-tunes that keep
the same class ordering) — every script filters by hardcoded COCO class id
(0=person, 56=chair, 60=dining table, 67=cell phone, etc.), so a model with a
different class list/order will silently misclassify rather than error.

Resolved once at import time from the top-level models/ directory
(docker-compose.*.yml: ./models:/app/models), NOT scripts/detection/models/.
The two look interchangeable but are not: the bind mount only covers the
former, so it is the only one that survives a container recreate. Every one
of these scripts originally resolved its path as
`os.path.join(BASE_DIR, "models", "<file>.pt")` with
`BASE_DIR = os.path.dirname(os.path.abspath(__file__))` — i.e.
scripts/detection/models/, which is plain container-local storage. That
directory is empty on every fresh container, so first use each time silently
fell back to Ultralytics' GitHub auto-download instead of the weights an
operator placed on the host — the same "code points at the ephemeral path,
not the persistent bind mount" bug class as the vLLM HF_HOME cache (see
start_vllm.sh). docker-entrypoint.sh's own model check already assumed
models/ relative to WORKDIR (/app/models) is canonical; the Python side just
never matched it. Dropping a new .pt file into the host's ./models/ and
changing the env var is now enough to switch models fleet-wide with no code
change, no image rebuild, and no re-download on recreate.
"""
import logging
import os

_DETECTION_DIR = os.path.dirname(os.path.abspath(__file__))  # .../scripts/detection
_MODELS_DIR = os.path.normpath(os.path.join(_DETECTION_DIR, "..", "..", "models"))  # .../models (bind-mounted)

DETECTION_MODEL_PATH = os.path.join(
    _MODELS_DIR, os.environ.get("YOLO_DETECTION_MODEL", "yolo26m.pt")
)


def _finetuned(env_name, default_file):
    """Fine-tuned model path, or the stock COCO model if the file is missing."""
    path = os.path.join(_MODELS_DIR, os.environ.get(env_name, default_file))
    if os.path.isfile(path):
        return path
    logging.getLogger(__name__).error(
        f"[model_config] {path} not found ({env_name}) — falling back to {DETECTION_MODEL_PATH}")
    return DETECTION_MODEL_PATH


PERSON_MODEL_PATH = _finetuned("YOLO_PERSON_MODEL", "monitor_person_model.pt")
PHONE_MODEL_PATH = _finetuned("YOLO_PHONE_MODEL", "phone_model.pt")

INVERSION_MODEL_PATH = os.path.join(
    _MODELS_DIR, os.environ.get("YOLO_INVERSION_MODEL", "yolo26n.pt")
)
