"""Write the nvinfer config for each model from its template + the manifest.

The manifest is the single source of truth (paths, batch size, precision,
threshold, class count); templates only hold each model's preprocessing.
"""
import os
import tempfile

PARSER_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parsers", "libnvdsinfer_pathb.so")

# nvinfer network-mode: 0 FP32, 1 INT8, 2 FP16. FP8 has no nvinfer mode — build
# the engine with trtexec (export/build_engines.sh) and nvinfer loads it as-is.
_NETWORK_MODE = {"fp32": 0, "int8": 1, "fp16": 2, "fp8": 2}


def render(spec, manifest, batch, gpu_id=0):
    with open(spec.nvinfer_template) as f:
        text = f.read()
    return text.format(
        gpu_id=gpu_id,
        gie_id=spec.gie_id,
        onnx=spec.onnx,
        engine=spec.engine,
        batch=batch,
        network_mode=_NETWORK_MODE[spec.precision],
        num_classes=spec.num_classes,
        threshold=manifest.floor_conf(spec.name),
        parser=spec.parser,
        parser_lib=PARSER_LIB,
    )


def write_configs(model_names, manifest, batch, out_dir=None, gpu_id=0):
    """Render configs for `model_names`; returns {model name: config path}."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="pathb_nvinfer_")
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for name in model_names:
        spec = manifest.models[name]
        path = os.path.join(out_dir, f"nvinfer_{name}.txt")
        with open(path, "w") as f:
            f.write(render(spec, manifest, min(batch, spec.max_batch), gpu_id))
        paths[name] = path
    return paths
