# Shared stream session per shift: plan

Status: plan, not started (written 6 Oct 2026, to be built from 7 Oct).

## Goal

Hold **one live stream per camera per worker** for a whole shift block and let every analytic
read the frames it needs from it, by camera type and its own time window. This replaces today's
round-robin (each lab camera seen 1–3 times an hour) without multiplying streams per analytic.

## Today

- Inside one run the design already works this way: `master_pipeline` opens one capture per camera,
  builds the camera's analytic set from camera types (`cam_scripts` → `enabled_scripts`,
  `master_pipeline.py` ~L1422), and `unified_detector` runs the person model once and fans the
  result out to those analytics only.
- But `poller._group_detection_assignments` merges only analytics with the **identical** window
  (key `grp-{exam}-{shift}-{link}-{start}-{end}`). Different windows are separate processes with
  their own streams. Shift 2 on 6 Oct, per worker: Monitor Tampering ~280 + 2 Crowd runs ~410 +
  core group ~530 → up to ~1,220 streams at 13:30–13:45 if all were held open.
- Round-robin visits ~15–20 cameras a minute per process; combined group sees each lab camera every
  36–60 min. Invigilator STATIC (needs checks ≤ 3 min apart for 7 min) cannot fire.

## Target design

```
worker (~530 scheduled cameras)
  Stream pool: 1 stream per camera (substream; main stream while Phone is active on it)
    opened when ANY active analytic needs the camera, closed when none does
        │ frame + active analytics for this camera (by type and time window)
        ▼
  Unified detector: person model once → fan-out
    Monitor Tamper  own window  Inside Lab        every frame (1–2 fps cap)
    Crowd           own window  Entry / Outside   every frequency/3 (10–60 s)
    Crowd < 5       own window  Inside Lab        "
    Invigilator     own window  Inside Lab        "
    Phone           own window  Lab, Server Room  main stream, keyframes
```

Peak streams per worker = cameras needed at that moment (≤ ~530 on today's schedule), independent
of the number of analytics.

## Changes

| # | Change | File(s) | Notes |
|---|---|---|---|
| 1 | Group by `(exam, shift, link_type)` into one **shift session**; keep each member's own start/end | `app/poller.py` | members carry `scheduled_at` / `end_at`, camera types, frequency, sms flag |
| 2 | Time-aware routing: every few s recompute each camera's active analytics from the windows; open stream when set becomes non-empty, close when empty; session ends after the last window | `pipeline/master_pipeline.py` (producer + a session scheduler) | camera list = union of members' camera types (CAMERA_CLASSIFICATIONS_<X>) |
| 3 | Per-analytic sampling: evaluate a camera for an analytic only when due (`custom_frequency_minutes` [stored in s] ÷ 3, clamp 10–60 s); Monitor Tampering every frame | `unified_detector.py` | per (camera, script) "next due" in existing state dict |
| 4 | Stream type per camera: substream (`DSS_STREAM_TYPE=2`) by default, main stream when Phone is active; keyframe-only decode option for main | producer, `get_rtsp_from_dss` | re-apply the 4 Oct `DSS_STREAM_TYPE` setting (backup in old scratchpad) |
| 5 | Per-analytic status: each assignment RUNNING → COMPLETED at its own end; adding an analytic mid-shift joins the running session (no full restart) | `poller.py`, `script_runner.py` | today the group starts/ends together and a membership change relaunches it |
| 6 | Reconnects: random 5–30 s delay, ≤ 2 reconnects per DVR per 10 s, back-off for DVRs that drop > 5×/10 min and for "MTS server is not ready"; fresh StartVideo through the token server on every reconnect | producer | open streams survive the ~31-min Camview session expiry (measured) |
| 7 | Weighted camera split (w4 has 64 cores) | `app/camera_distributor.py` | weights from `.env`; same crc32 hash |
| 8 | Watchdog: stop opening streams above CPU / memory limits | producer | as in the 4 Oct test script |

Unchanged: scene classification (vLLM, one snapshot), alert saving / DB reporting / push / SMS,
every analytic's rules, 2-hit gate and cooldowns, proximity repeat counter, token server.

## Budget (measured)

- 800 substreams on one 24-core worker: CPU 61–71 %, ~37–41 GB free, ~197 Mbps (4 Oct).
- 250 main streams (Monitor Tampering): CPU 62–96 % (6 Oct) → main stream only for Phone.
- ~530 substreams per worker ≈ 40–45 % CPU.
- Person model needs ≈ 13 frames/s per worker at frequency/3 sampling (capacity ~148).
- Monitor Tampering RF-DETR is the heaviest (57 ms CPU/frame): cap 1–2 fps per camera.

## Risks

- One process per worker: a crash stops all analytics there until the poller's crash-retry
  relaunches it; reconnect burst must be spread (change 6).
- Camview total capacity at ~2,100 substreams fleet-wide is untested (1,100 main streams worked).
- Camview refuses logins ~21:27 onward at night (5 and 6 Oct) — outside exam hours.
- Network: confirm whether ~197 Mbps on worker-0 is per worker or shared.

## Test plan (before any exam day)

1. Unit: routing table vs windows (analytic on/off at boundaries), stream open/close, per-analytic
   sampling, per-member status.
2. SSC_test (exam 43), all 4 workers, 2 h, outside exam hours, with Monitor Tampering: CPU / RAM /
   network per worker, streams held, drops per DVR, reconnect bursts, alerts per analytic.
3. Compare alert quality with round-robin on the same cameras (Crowd < 5, Invigilator STATIC,
   Intruder, Proximity).

## Deploy

Workers' code is baked into the image (no bind mount): copy changed files into each container and
`docker restart` (never recreate without re-copying). Feature flag `SHIFT_SESSION=0/1` so
round-robin stays the fallback.

## Estimate

~1.5–2 days development + 2 h soak test.
