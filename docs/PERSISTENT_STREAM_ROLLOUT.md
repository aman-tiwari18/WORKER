# Persistent-stream capture: production rollout plan

As of 5 Oct 2026. Tests ran on worker-0 against SSC_test (exam 43), a copy of exam 42's cameras.
Production code and settings are unchanged (see "State of the code" at the end).

## Summary

Switch detection capture from round-robin to kept-open substreams. On 4–5 Oct one worker analysed
every camera about every 7–35 s this way, against every 13–65 min with round-robin today.

- **Today:** round-robin opens a stream, grabs 2 frames and closes it. Each worker process manages
  about 22 cameras a minute, so big runs see each camera once or twice per shift window.
- **Tested:** one stream per camera kept open for the whole window (as Monitor Tampering already does),
  on the DVR substream. Worker-0 held 800 streams with 4 analytics at 67–71% CPU, and 1,000 streams with
  exam 42's 11 analytics at 91–93% CPU.
- **Recommendation:** about 800 kept-open substreams per worker, two detector processes per worker,
  Phone kept on the main stream, and a fleet-wide trial before any exam shift.

## Problem today

Round-robin capture tops out at about 22 cameras per minute per worker process, so most analytics
sample each camera once or twice per window.

| Run on 1 Oct (worker-1) | Cameras | Visits per 5 min | Each camera seen every |
| --- | --- | --- | --- |
| Crowd (Entry/Exit), Entry cameras | 15 | ~110 | 0.7 min |
| Phone, Server Room | 39–44 | ~103 | 2 min |
| Intruder | 318 | ~118 | 13.5 min |
| Invigilator | 319 | ~101 | 16 min |
| Combined 13:30 group (7 analytics) | 470–538 | ~37–62 | 38–67 min |

Causes found:

1. **6 streams per Camview server.** `MAX_STREAMS_PER_DVR=6` is keyed by Camview server, not by DVR
   device. Exam 42's 2,816 accessible cameras sit on 3 servers (d14: 79 DVRs, d15: 47, d11: 2), so a
   process opens about 12 streams at once.
2. **Ports re-probed on every visit.** Each StartVideo re-tests 3–4 stream addresses, most of them
   blocked ports with a 2 s timeout each.
3. **DVRs hold closed streams.** A cut stream stays reserved for about 4–5 minutes. Opening and closing
   streams quickly fills the DVR's slots, so more concurrency only adds refusals.

The GPU is not the limit: it ran at 1–3% during round-robin runs.

## Test results

### 1. Round-robin with higher limits (does not help)

2,164 cameras, the 7 analytics of the 13:30 group.

| Run | Limits | Captured / min | Attempts / min | Failed |
| --- | --- | --- | --- | --- |
| A (today) | 6 per server | 22–24 | ~37 | ~35% |
| B | 6 per DVR, 40 per server, ports remembered | 16–19 | ~77 | ~78% |
| B2 | 2 per DVR, 40 per server | 16–18 | ~74 | ~77% |
| B3 | 2 per DVR, 80 per server | 11–16 | ~144 | ~92% |
| B4 | 2 per DVR, 120 per server | 8–15 | ~168 | ~95% |

More streams at once only adds refusals; the ceiling is about 20 new streams a minute.

### 2. Kept-open streams, 800 on one worker

| | Main stream, Zero Person | Substream, Zero Person | Substream, 4 analytics |
| --- | --- | --- | --- |
| Cameras connected | 668 (stopped at 48 s) | 787 | 790 |
| Cameras failed | 147 | 189 | 197 |
| CPU, steady | 100% (stopped) | 61–65% | 67–71% |
| Free memory | 36 GB | ~41 GB | ~37 GB |
| Network in | 152 Mbps | ~196 Mbps | ~197 Mbps |
| Frames analysed | — | ~3,150 / 30 s | ~3,000–3,260 / 30 s |

The 4 analytics were Zero Person, Improper Furniture, Camera Tampering, Camera Inversion; all four
analysed frames and raised alerts. The 4-analytic run was stopped at 4½ min by the watchdog when a wave
of stream drops caused mass reconnects (CPU 94%).

Connect time (800 substreams): 50% in 36 s, 90% in 61 s, 100% in 128 s. Full analysis speed after
about 2½–3 min.

### 3. Kept-open substreams, 1,000 on one worker, exam 42's analytics

11 analytics (all of exam 42's except Monitor Tampering, whose RF-DETR model did not load on worker-0
until the 5 Oct library-path fix), production person model, exam 42's camera types and frequencies.

| | Value |
| --- | --- |
| Cameras connected | 978 of 1,000 (90% in ~80 s) |
| CPU, steady | 91–93% |
| Free memory | ~21 GB (~90 GB in use) |
| Network in | ~197 Mbps (same as at 800) |
| Frames analysed | ~800–860 / 30 s, each camera every ~35 s |
| Cameras with a dropped stream | 616 |

Alerts: Crowd 600, Zero Person 586, Invigilator 452, Intruder 26, Inversion 15, Crowd < 5 14,
Entry Exit 2, Phone 2 (empty rooms at night).

### 4. Stream drops

Not a fixed expiry. 26–35% of streams dropped at least once, mostly 2–3 min after connecting, and by
DVR: on some DVRs (e.g. 1000007–1000014, 1000029, 1000032) every camera drops at the same moment;
11–13 DVRs never dropped. Dropped cameras reconnect.

### 5. Model cost (worker-1, batch 24)

| Model | Used by | GPU memory | Speed | CPU per frame |
| --- | --- | --- | --- | --- |
| yolov10m | (test runs only) | 2.5 GB | 418 frames/s | 2.4 ms (1 core) |
| person_monitor_yolo26m | production people analytics | 5.9 GB | 148 frames/s | 6.8 ms (1 core) |
| mobile (960 px) | Phone | 5.9 GB | 172 frames/s | 5.8 ms (1 core) |
| person_monitor_rfdetr | Monitor Tampering | 10.3 GB | 274 frames/s | 57 ms (~16 cores) |

The detector is bound by one CPU core (pre/post-processing), not by the GPU.

### 6. Substream resolution

Main stream 1280×720 on the sampled cameras; substreams vary by DVR: 1280×720, 720×576, 640×480,
352×240. Fine for people analytics; too small for Phone.

## Changes to apply for production

1. **Capture mode.** Run detection analytics as kept-open streams instead of round-robin:
   `CONTINUOUS_SCRIPTS` = the people analytics (crowd, crowd_five, crowd_five_invigilator, crowd_zero,
   entry_exit, intruder, invigilator, proximity, server, furniture) plus tamper/inversion.
   `SAMPLE_FPS=0.25` (one frame per camera every 4 s).
2. **Substream for people analytics.** `DSS_STREAM_TYPE=2` (new setting, default `1`). Phone stays on
   the main stream, in its own process.
3. **Stream budget per worker.** `MAX_CONCURRENT` ≈ 800 kept-open substreams per worker, including
   Monitor Tampering's streams.
4. **Gradual reconnects.** Spread reconnects of dropped streams (jittered delay) so a DVR dropping all
   its cameras does not spike CPU. Code change in `rtsp_producer`.
5. **Two detector processes per worker.** The unified detector is bound by one CPU core; a second
   process (or GPU-side pre-processing) uses the idle GPU. Code change.
6. **Start windows early.** Connecting 800 streams takes ~1–2 min and full analysis speed ~3 min, so
   a run should start ~3 min before the window it covers.
7. **Alert frequency.** Unchanged: `custom_frequency_minutes` (stored in seconds) sets the gap between
   saved alerts per camera; it does not change how often frames are analysed.

## Rollout plan, risks and open questions

1. **Fleet-wide trial on SSC_test** with all 5 workers at once, alongside Monitor Tampering's
   ~1,500 streams, outside exam hours. Measure total streams the Camview servers hold.
2. **Network cap.** Worker-0 read ~197 Mbps at both 800 and 1,000 streams. Confirm whether that is
   worker-0's link or the relay before sizing above 800.
3. **DVRs that drop every few minutes.** Ask the DVR owners about the session timeout; meanwhile
   gradual reconnects.
4. **Memory.** ~60 MB per stream process (~50 GB at 800). Running cameras as threads would cut this.
   vLLM classification needs ~79 GB of GPU on the same worker; keep classification and capture windows
   apart.
5. **Worker-4.** Shared GPU with a 100 GB cap; size its stream budget lower.
6. **Worker-0 RF-DETR (fixed 5 Oct).** Worker-0's venv still holds old CUDA-12 libraries
   (nvidia-cuda-nvrtc-cu12 12.4, cudnn-cu12 9.1) next to torch's CUDA-13 ones, so cuDNN loaded NVRTC 12.4
   and fp16 attention failed ("No valid execution plans built"). Fixed by `LD_LIBRARY_PATH=<venv>/nvidia/cu13/lib`
   in worker-0's `.env`. The NVIDIA kernel thread `nv_open_q` spinning one core is unrelated and still open.

## Test scripts (in the repo, not run by production)

- `scripts/detection/rr_bottleneck_test.sh` — round-robin A/B runs.
- `scripts/detection/persistent_stream_test.sh` — kept-open stream test with watchdog (CPU, memory) and
  cleanup of leftover processes.
- `scripts/detection/exam42_stream_test.sh` — exam 42's analytics on kept-open streams.
- `scripts/detection/centre_test.py` — test analytics on chosen centres, on worker-1.

These rely on the test settings (`RR_LIMIT_KEY`, `MAX_STREAMS_PER_SERVER`, `PORT_PROBE_CACHE_S`,
`RR_STATS_EVERY_S`, `DSS_STREAM_TYPE`), which were removed from production `master_pipeline.py` after
testing; re-apply them (backup in the session scratchpad) before re-running the tests.
