"""Load and query the Plan B model manifest (pathb/models.yaml)."""
import os
from dataclasses import dataclass, field

import yaml

DETECTION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MANIFEST = os.path.join(DETECTION_DIR, "pathb", "models.yaml")
# Same folder model_config.py uses: repo-root models/ (bind-mounted in containers).
MODELS_DIR = os.environ.get("PATHB_MODELS_DIR") or os.path.normpath(
    os.path.join(DETECTION_DIR, "..", "..", "models"))


@dataclass(frozen=True)
class ModelSpec:
    name: str
    gie_id: int
    parser: str
    weights: str
    onnx: str
    engine: str
    nvinfer_template: str
    input_size: int
    num_classes: int
    precision: str
    max_batch: int
    conf_threshold: float
    enabled: bool
    class_map: dict = field(default=None)   # None = identity (COCO ids)

    def to_coco(self, cls):
        """Model class index -> COCO id the rules expect; None = drop this box."""
        if self.class_map is None:
            return int(cls)
        return self.class_map.get(int(cls))


class Manifest:
    def __init__(self, models, analytics, analytic_min_conf=None):
        self.models = models            # name -> ModelSpec
        self.analytics = analytics      # analytic -> [model names]
        self.analytic_min_conf = analytic_min_conf or {}
        self.by_gie_id = {m.gie_id: m for m in models.values()}

    def min_conf(self, analytic, model_name):
        """Confidence an analytic requires from a model's boxes."""
        return self.analytic_min_conf.get(analytic, self.models[model_name].conf_threshold)

    def floor_conf(self, model_name):
        """Lowest confidence any analytic needs from this model — what the engine must keep."""
        base = self.models[model_name].conf_threshold
        lows = [c for a, c in self.analytic_min_conf.items() if model_name in self.models_for_analytic(a)]
        return min([base] + lows)

    def models_for_analytic(self, analytic):
        names = self.analytics.get(analytic, self.analytics.get("default", []))
        return tuple(n for n in names if n in self.models and self.models[n].enabled)

    def models_for(self, analytics):
        """Models a camera needs for its set of analytics, in gie_id order."""
        needed = {n for a in analytics for n in self.models_for_analytic(a)}
        return tuple(sorted(needed, key=lambda n: self.models[n].gie_id))


def _in(base, path):
    return path if os.path.isabs(path) else os.path.join(base, path)


def load_manifest(path=None):
    path = path or os.environ.get("PATHB_MANIFEST") or DEFAULT_MANIFEST
    with open(path) as f:
        raw = yaml.safe_load(f)

    models = {}
    gie_ids = set()
    for name, m in (raw.get("models") or {}).items():
        cmap = m.get("class_map", "coco")
        if cmap in (None, "coco"):
            cmap = None
        elif isinstance(cmap, dict):
            cmap = {int(k): int(v) for k, v in cmap.items()}
        else:
            raise ValueError(f"model {name}: class_map must be 'coco' or a mapping")
        spec = ModelSpec(
            name=name,
            gie_id=int(m["gie_id"]),
            parser=m["parser"],
            weights=_in(MODELS_DIR, m["weights"]),
            onnx=_in(MODELS_DIR, m["onnx"]),
            engine=_in(MODELS_DIR, m["engine"]),
            nvinfer_template=_in(DETECTION_DIR, m["nvinfer_template"]),
            input_size=int(m["input_size"]),
            num_classes=int(m["num_classes"]),
            precision=str(m.get("precision", "fp16")).lower(),
            max_batch=int(m.get("max_batch", 16)),
            conf_threshold=float(m.get("conf_threshold", 0.45)),
            enabled=bool(m.get("enabled", True)),
            class_map=cmap,
        )
        if spec.gie_id in gie_ids:
            raise ValueError(f"duplicate gie_id {spec.gie_id}")
        if spec.precision == "int8" and spec.parser == "NvDsInferParseRFDETR":
            raise ValueError("RF-DETR must not run INT8 (accuracy); use fp16 or fp8")
        gie_ids.add(spec.gie_id)
        models[name] = spec

    analytics = {k: list(v) for k, v in (raw.get("analytics") or {}).items()}
    for a, names in analytics.items():
        unknown = [n for n in names if n not in models]
        if unknown:
            raise ValueError(f"analytic {a} references unknown models {unknown}")
    if "default" not in analytics:
        raise ValueError("manifest needs an analytics.default entry")
    min_conf = {k: float(v) for k, v in (raw.get("analytic_min_conf") or {}).items()}
    return Manifest(models, analytics, min_conf)
