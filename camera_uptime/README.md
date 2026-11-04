# camera-uptime-service

An independent checker. During the scheduled shift hours of live exams it checks every scheduled
camera, captures one frame, and records whether the camera was available. It keeps that uptime
data for every shift.

**What it does not do:** no vLLM and no camera classification. No GPU, no scene labels. It never
writes `camera_links_*` or `camera_classification_*`. It imports nothing from `script-server`.

## How it works

**What is checked**
- A shift is live inside its `exam_shifts` start/end on an exam day.
- Exam days are the exam's date range minus `skip_days` / `skip_dates`, then the
  `exam_slot_schedule` gate.
- Checking starts after a 5-minute hold (`START_HOLD_MIN`) while detection opens its streams.
- Cameras come from `camera_links_rtsp` / `camera_links_hls` plus the centre schedule. Every
  status is included.

**Pacing**
- One camera at a time per Camview server (`MAX_INFLIGHT_PER_SERVER`), plus one HLS lane.
- At most `MAX_INFLIGHT_PER_DVR` checks at a time per DVR, with at least `DVR_MIN_GAP_S` between check
  starts on the same DVR.
- At most `MAX_NEW_STREAMS_PER_MIN` new RTSP streams per minute overall.
- At most `HLS_REQ_PER_S` HTTP requests per second to TCS.

**RTSP checks**
- The token comes from `camview-token-server` (:8090). The service never logs in, evicts, or
  forces a refresh.
- Then: StartVideo on the main stream, then ffmpeg grabs one frame.
- StopVideo is off (`STOP_VIDEO=0`).

**HLS checks:** portal page → `.m3u8`, then ffmpeg grabs one frame.

**Results**

| Result | Meaning |
|---|---|
| `UP` | A frame was captured |
| `DOWN` | Camera-side failure |
| `UNKNOWN` | Infrastructure failure (token, DVR/DSS, CDN block). Excluded from uptime % |

A uniform or black frame sets `frame_blank`. This is a pixel-statistics flag only: the camera
stays `UP`.

**Tables**

| Table | Contents |
|---|---|
| `camera_uptime_runs` | One row per exam / shift / date |
| `camera_uptime_checks` | Every check |
| `camera_uptime_summary` | One row per camera per run |
| `camera_uptime_events` | Pauses and backoffs |

The latest frame per camera per shift is saved as
`FRAMES_ROOT/<exam>/<date>/<shift>/<camera>.jpg`.

**Viewing:** the admin page is **Camera Uptime** (sidebar). It has an Excel download and is served by the
backend at `/api/camera-uptime/*`.

## Run (new setup: next to the workers on every Fleet server)

The checker is part of the worker image (`/app/camera_uptime`) and runs as a second container beside the
workers. It needs no GPU.

| Where | How it gets there | Container |
|---|---|---|
| Main server | `deploy/docker-compose.uat.yml` service `camera-uptime` (CI deploy) | `cctv_uat_camera_uptime`, ops API `127.0.0.1:8096` |
| Server added with **Add server** (LAN) | the bootstrap writes its compose block and DB login | `cctv_camera_uptime_v2` |
| Server enrolled earlier | the Fleet agent (v7) adds the same block on its next **update** | `cctv_camera_uptime_v2` |
| Remote (internet) server | not installed: it has no database access | — |

- **On/off:** Fleet page → Settings → `UPTIME_ENABLED=1` for all servers, then **update**. Default `0`: the
  container stays up but touches no camera and no database.
- **Camera split:** `UPTIME_SHARD_COUNT=0` (auto). Each checker heartbeats into `camera_uptime_instances` and
  takes its place among the live ones, so adding, stopping or removing a server rebalances the DVRs within
  about a minute. A DVR is always checked by exactly one server, so the per-DVR pacing holds fleet-wide.
- **Database:** tables come from backend migration `051_camera_uptime.sql`, in the same database as everything
  else. Servers log in with the backend's own `DB_USER` / `DB_PASSWORD`. Override them with `UPTIME_DB_USER` /
  `UPTIME_DB_PASSWORD`, and set `UPTIME_DB_HOST` / `UPTIME_DB_PORT` (default 192.168.19.30:3308) if needed.
- **Pacing:** Fleet settings `UPTIME_RTSP_STREAM_TYPE` (2), `UPTIME_MAX_INFLIGHT_PER_SERVER` (8),
  `UPTIME_MAX_INFLIGHT_PER_DVR` (2), `UPTIME_DVR_MIN_GAP_S` (0), `UPTIME_MAX_NEW_STREAMS_PER_MIN` (250),
  `UPTIME_CARRY_OVER_AFTER_S` (300) and `UPTIME_FRAME_SAVE_THREADS` (8) override the defaults in brackets.
- **Frames:** written to the shared frames folder under `uptime_frames/`. The backend reads them there
  (`FRAMES_DIR/uptime_frames`).
- **Cameras tab:** a labelled RTSP camera found UP is marked Accessible with its new frame
  (`uptime/promote.py`), saved under `classification_frames/uptime/`. `PROMOTE_TO_CAMERAS=0` turns this off.

Local run, outside Docker: `pip install -r requirements.txt`, then `python run.py run` with `DB_*` (or
`UPTIME_DB_*`) and `CAMVIEW_TOKEN_SERVER` set.

## Commands

```bash
venv/bin/python -m uptime dry-run [--at "2026-11-02 13:30"]   # live runs + roster per lane; touches no camera
venv/bin/python -m uptime check-one --camera-id 291338 --save  # one real RTSP check, no DB writes
venv/bin/python -m uptime check-one --exam-id 44 --ch-code X   # one real HLS check
venv/bin/python -m uptime rebuild-summary --run-id N           # recompute a run's summary from raw checks
UPTIME_TEST_DB=1 venv/bin/python -m pytest -q tests            # unit tests (+ DB tests, rolled back)

curl -s 127.0.0.1:8096/status | python3 -m json.tool             # lanes, pass progress, ETA, pauses
curl -s -XPOST 127.0.0.1:8096/pause                              # kill switch (or: touch PAUSE)
curl -s -XPOST 127.0.0.1:8096/resume
docker logs -f cctv_camera_uptime_v2      # main server: cctv_uat_camera_uptime
```

## Tuning (Fleet settings, then **update**; the `UPTIME_*` names above for pacing)

- **Pilot on one server:** `ONLY_SERVERS=d11.camview.io`. Leave it empty for all servers. Use
  `INCLUDE_HLS=1` for HLS exams.
- **More checks per shift:** `MAX_INFLIGHT_PER_SERVER=2` doubles the checks per shift.
  - Each DVR still sees one check at a time.
  - Raise `MAX_NEW_STREAMS_PER_MIN` to match.
- **HLS rate:** `HLS_REQ_PER_S` is capped at 1. Each HLS check costs about 3 requests.
- **Stream type:** `RTSP_STREAM_TYPE=1` is the main stream; detection's kept-open sessions use the
  substream (2).
- **`STOP_VIDEO`:** turn it on only after testing next to detection. StopVideo targets the shared
  session plus channel, not this checker's own stream.
