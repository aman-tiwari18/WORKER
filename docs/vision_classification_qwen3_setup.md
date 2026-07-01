# Vision Classification Qwen3-VL Setup

## Summary

- Replaced the Ollama-based `llava:7b` call in `scripts/detection/vision_classification.py` with direct local Hugging Face inference.
- Target model: `wangkanai/qwen3-vl-8b-instruct`.
- Runtime uses CUDA automatically when available.
- The script expects a local assembled model directory at `script-server/models/qwen3-vl-8b-instruct`.

## Local Model Layout

The runtime directory must contain:

- `config.json`
- `generation_config.json`
- `preprocessor_config.json`
- `tokenizer.json`
- `tokenizer_config.json`
- `vocab.json`
- `merges.txt`
- `chat_template.json`
- `video_preprocessor_config.json`
- `model.safetensors`

The metadata files come from `Qwen/Qwen3-VL-8B-Instruct`.
The weight file comes from `wangkanai/qwen3-vl-8b-instruct` and must be renamed to `model.safetensors`.

## Download Commands

Run these from `script-server` using the project venv:

```bash
./venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id='Qwen/Qwen3-VL-8B-Instruct',
    local_dir='models/qwen3-vl-8b-instruct',
    allow_patterns=[
        'config.json',
        'generation_config.json',
        'preprocessor_config.json',
        'tokenizer.json',
        'tokenizer_config.json',
        'vocab.json',
        'merges.txt',
        'chat_template.json',
        'video_preprocessor_config.json',
    ],
    local_dir_use_symlinks=False,
)

snapshot_download(
    repo_id='wangkanai/qwen3-vl-8b-instruct',
    local_dir='models/qwen3-vl-8b-instruct-download',
    allow_patterns=['qwen3-vl-8b-instruct-abliterated.safetensors'],
    local_dir_use_symlinks=False,
)
PY

mv models/qwen3-vl-8b-instruct-download/qwen3-vl-8b-instruct-abliterated.safetensors \
   models/qwen3-vl-8b-instruct/model.safetensors

rmdir models/qwen3-vl-8b-instruct-download
```

## Runtime Notes

- GPU device selection is automatic: `cuda:0` when CUDA is available.
- Model dtype is `float16` on GPU and `float32` on CPU.
- Inference is guarded by a lock so the existing multithreaded camera pipeline does not run concurrent generations on the same model instance.
- If model files are missing, the script raises a clear local asset error instead of silently falling back to Ollama.

## Dependency Changes

Added to `requirements.txt`:

- `transformers==5.3.0`
- `accelerate==1.13.0`
- `huggingface_hub==1.7.2`
- `safetensors==0.7.0`
