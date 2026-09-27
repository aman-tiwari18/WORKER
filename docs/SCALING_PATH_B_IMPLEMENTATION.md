# Plan B (SCALE_PATH=B): DeepStream + TensorRT detection

Branch: `scale_path_b` (worker repo).
Plan A is the current path: round-robin + OpenCV/FFmpeg decode + Ultralytics PyTorch.
Plan A is still the default and nothing changes unless `SCALE_PATH=B` is set.

## 1. What changes, in one picture

```
Plan A (today)                              Plan B
--------------                              ------
per camera: open RTSP/HLS (CPU/FFmpeg)      many cameras -> ONE DeepStream pipeline per model set
  grab frame -> close                         nvurisrcbin (NVDEC, keyframes only)
  YOLO (PyTorch, per analytic)                -> nvstreammux (batch)
  rule -> DB/SMS                              -> nvinfer YOLO26 (TensorRT) [-> nvinfer RF-DETR]
                                              -> probe: boxes (+ frame only when a rule is due)
                                              -> EvaluatorPool -> SAME rules -> DB/SMS
```

- Decode moves from CPU to the GPU video engine (NVDEC).
- The model runs once per frame for all analytics of that camera (TensorRT, batched), not once per analytic.
- The analytics rules (`unified_detector.EVALUATORS`) are **unchanged**. Plan B hands them a result object that looks like an Ultralytics result, so they give the same answers (tested, see §6).
- Streams stay open (held) when they fit. Otherwise they rotate with a dwell time, like round-robin but with a limit on how many we open per DSS server and per recorder.

## 2. How to turn it on

| Setting | Where | Effect |
|---|---|---|
| `SCALE_PATH=B` | worker env | `run_rtsp_pipeline` / `run_hls_pipeline` in `master_pipeline.py` go to Plan B |
| `SCALE_PATH=A` or unset | worker env | old code (`_run_rtsp_pipeline_legacy` / `_run_hls_pipeline_legacy`) |

Plan B needs the DeepStream image: `deploy/deepstream/Dockerfile` (sets `SCALE_PATH=B` by default).
The fleet heartbeat now reports `scale_path` in `capabilities`, so the GPU/fleet page shows which workers run B.

Cameras whose analytics are not served by the shared detector (classical-CV ones such as tamper/inversion) are split off by `split_for_pathb()` and still run on Plan A in the same worker.

## 3. Files

All new code is under `scripts/detection/pathb/`.

| File | Job |
|---|---|
| `models.yaml` | Which models exist, their engines, class maps to COCO ids, which analytics use which models, per-analytic min confidence (`qp_box: 0.30`) |
| `manifest.py` | Loads and checks `models.yaml` (duplicate gie ids, INT8 RF-DETR rejected) |
| `configs/nvinfer_*.txt` | nvinfer templates; filled from the manifest at start-up by `nvinfer_config.py` |
| `parsers/` | C++ box parsers for YOLO26 end-to-end and RF-DETR (`make`, `make test`) |
| `boxes.py` | Turns nvinfer boxes into an Ultralytics-like result (`boxes.cls/conf/xyxy`, `names`, `from_pathb=True`) |
| `ds_pipeline.py` | The DeepStream pipeline: add/remove sources at runtime, probe, bus errors |
| `stream_manager.py` | Which cameras are open: held vs rotating, dwell, stall detection, backoff |
| `stream_budget.py` | Max streams per DSS server / per recorder / per HLS origin, circuit breaker |
| `sources.py` | Camera key + URL resolve (DSS token, StartVideo cache, HLS) reusing Plan A helpers |
| `evaluator.py` | Thread pool that runs the rules; same camera always on the same worker (order kept) |
| `runner.py` | `run_pathb()` wires it all together; env knobs in its docstring |
| `export/` | Export ONNX (YOLO26, RF-DETR), build TensorRT engines, validate engine vs PyTorch |
| `decode_ref.py` | Python copy of the parser maths, used by the validator |
| `tools/run_video_test.py` | Run the full pipeline on video files (dry run, saves annotated hits) |
| `tests/` | Unit + end-to-end tests (no DeepStream needed) |

Changed files outside `pathb/`:

- `pipeline/master_pipeline.py`: renamed old runners to `_legacy`, added the `SCALE_PATH` dispatcher and `_pathb_helpers()`.
- `qp_box_det.py`: uses the shared boxes only when they come from Plan B (`from_pathb`); Plan A behaviour is identical.
- `app/fleet_agent.py`: `scale_path` capability; `unified_detector` default now matches the pipeline ("1").
- `deploy/deepstream/Dockerfile`: new image.

## 4. Settings (env, all optional)

| Env | Default | Meaning |
|---|---|---|
| `PATHB_MAX_SOURCES_PER_PIPELINE` | 64 | streams open at once per model group |
| `PATHB_MAX_STREAMS_PER_DSS` | 50 | RTSP: per DSS server, per worker. Raise slowly with a ramp test |
| `PATHB_MAX_STREAMS_PER_DEVICE` | 6 | RTSP: per exam-centre recorder |
| `PATHB_MAX_STREAMS_PER_HLS_ORIGIN` | 32 | HLS: per origin, per worker |
| `PATHB_PERSISTENT_ANALYTICS` | "" | these analytics are always held open |
| `PATHB_PERSISTENT_SHARE` | 0.5 | max share of slots for held cameras |
| `PATHB_DWELL_S` | 20 | time a rotating camera keeps its slot |
| `PATHB_STALL_S` | 15 | no frame this long = dead stream |
| `PATHB_EVAL_INTERVAL_S` | 2 | run the rules on a camera at most this often |
| `PATHB_KEYFRAMES_ONLY` | 1 | decode only keyframes (about 1 frame per GOP) |
| `PATHB_EVAL_WORKERS` / `PATHB_EVAL_QUEUE` | 8 / 64 | rule threads / queue size |
| `PATHB_BREAKER_FAILS` / `PATHB_BREAKER_COOLOFF_S` | 5 / 300 | server marked bad after N failures, for N seconds |
| `PATHB_NEW_STREAMMUX` | 1 | new nvstreammux: each camera keeps its own resolution (see §6.1). 0 = legacy mux |
| `PATHB_MUX_WIDTH` / `PATHB_MUX_HEIGHT` | 1920 / 1080 | legacy mux only: size every camera is resized to |
| `PATHB_GPU_ID` | 0 | GPU for decode + inference |
| `PATHB_DRY_RUN_DIR` | "" | tests: save annotated hits here, no DB/SMS |
| `PATHB_LOOP_FILES` | 0 | tests: loop `file://` sources |
| `PATHB_MODELS_DIR` | repo `models/` | where ONNX/engines live |

## 5. Build and run

```bash
# 1. image
docker build -f deploy/deepstream/Dockerfile -t cctv-worker:pathb .

# 2. inside the container: export + build engines (once per GPU type / TensorRT version)
cd scripts/detection
python -m pathb.export.export_yolo26            # -> models/pathb/yolo26.onnx
python -m pathb.export.build_engines --model yolo26   # -> models/pathb/yolo26_fp16.engine

# 3. check against PyTorch before using it (ONNX first, then the engine with --trt)
python -m pathb.export.validate_engine --model yolo26 --source /path/to/video.mp4
python -m pathb.export.validate_engine --model yolo26 --source /path/to/video.mp4 --trt

# 4. try on videos (no DB, no SMS)
python -m pathb.tools.run_video_test --video /path/to/videos \
    --analytics proximity crowd_five phone --seconds 300 --copies 8 --out /tmp/pathb_video_test

# 5. real run
SCALE_PATH=B python3 run.py
```

RF-DETR is off (`enabled: false`) until its weights and class map are final. To turn it on: set the real class map in `models.yaml`, export, build, validate, then set `enabled: true`.

## 6. What is tested and what is not

Tested on the dev host (no DeepStream needed):

- All 14 analytics rules give the **same output** with the Plan B result object as with a real Ultralytics result (`test_boxes_rules`).
- Manifest checks, per-analytic thresholds, class maps, stream budget, circuit breaker.
- Stream manager: held first then rotation, per-device budget, stall + backoff, dead camera backoff, error handling, eval interval.
- Runner end-to-end with a fake pipeline (4 hits written in dry-run).
- C++ parsers compiled and tested against a stub header.
- Python decode maths (letterbox, YOLO26, RF-DETR) used by the validator.

### 6.1 Test on real videos (27 Sep 2026)

6 lab CCTV clips ("Monitor change 1-6", 1448x1080 to 1914x984, H.264, 25-190 s),
copied from the dev server (192.168.19.45:/root/cctv/monitor_script/videos) to
`/data/test_videos`. Frame every 2 s = 244 frames, 1660 person boxes. No DeepStream:
the same YOLO26m in PyTorch, with Plan B's box adapter, thresholds and rules.

Person boxes and rule results compared with Plan A on the same frames:

| Plan B variant | person boxes found (recall / precision vs A) | rules agreeing with A |
|---|---|---|
| native frame (new streammux) | 100% / 100% | 100% on all 14 rules |
| resized to 1920x1080 (legacy streammux) | 91.7% / 93.8% | 92-100% (crowd, proximity, blind_spot, furniture ~93%) |
| resized with padding (legacy, enable-padding) | 87.8% / 92.9% | 90-100% |

For scale: re-saving a frame as JPEG q95 alone changes 3% of boxes. The resize
changes 8%, almost all low-confidence boxes (for boxes >= 0.6 it is 97.8%). Because
of this the pipeline now uses the **new nvstreammux** by default, which does not resize,
so nvinfer letterboxes from the camera's own frame like Plan A does.

Full real-time run of `run_pathb` (DeepStream replaced by OpenCV decode at real
speed + the same model), 12 cameras on 8 slots, 5 analytics, 3 minutes:
cameras rotated as designed (72 stream opens, 0 failures), 526 evaluations,
0 dropped, 0 errors, 36 hits saved with boxes in the right place.
Found and fixed a bug in the runner (frame callback did not pass the box space).

**Keyframes:** these clips have a keyframe only every 4-13 s. With
`PATHB_KEYFRAMES_ONLY=1` that is one frame per 4-13 s per camera, and rules need
2 hits in a row, so an alert could take 10-25 s. Check the GOP of the real DSS
cameras before using keyframes-only; if it is above ~2 s, use `PATHB_KEYFRAMES_ONLY=0`
(decode all frames on NVDEC, the rules still run every `PATHB_EVAL_INTERVAL_S`).

Run them:

```bash
cd scripts/detection
python -m pathb.tests.run_tests          # 16 tests
make -C pathb/parsers test
```

**Not tested yet** (needs the DeepStream container or new packages on the GPU host):

- Real DeepStream pipeline (`ds_pipeline.py`) with live RTSP/HLS and with video files,
  including the new streammux + nvvideoconvert frame copy at mixed resolutions.
- ONNX export and TensorRT engine build; engine accuracy vs PyTorch.
- RF-DETR export (weights and class map not final).
- How many streams one H200 really handles, and how many the DSS servers accept (ramp test).

## 7. Rollout

1. Build image, build engines, run `validate_engine` (recall/precision close to PyTorch).
2. `run_video_test` on recorded exam videos, compare hits with Plan A on the same videos.
3. One worker with `SCALE_PATH=B` on a small exam, low `PATHB_MAX_STREAMS_PER_DSS`; watch DSS load and the progress log.
4. Raise the limits step by step. Roll back = set `SCALE_PATH=A` and restart.

## 8. Things to know

- DeepStream, pyds and TensorRT versions must match. Changing one means rebuilding engines.
- Engines are tied to the GPU type and TensorRT version. Build them on the same kind of GPU they run on.
- vLLM scene classification is not in this image. Keep it on the normal worker image or GPUs.
- Keyframes-only decode means about 1 frame per GOP (often 1–2 s). Analytics that need every frame should set `PATHB_KEYFRAMES_ONLY=0` for that worker.
