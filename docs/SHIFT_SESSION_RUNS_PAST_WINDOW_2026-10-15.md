# Shift session kept running after its window ended (15 Oct 2026)

Written for: the CCTV dev team (backend, poller and worker owners).

## Symptom

- Assignment **#12901** (Intruder Detection, Shift 1, exam 42, shift id 160) has a window of **05:52–06:00** on 15 Oct.
- The UI showed it as **COMPLETED** (06:00:04).
- The intruder pipeline was still running at 08:59 IST, over 3 hours after the window ended.
  - Its detections were still being saved to the DB.
  - It was running on **all five workers** (w0, w2, w3, w4, w5).
- The shift itself is 10:00–11:00. The session started at 05:52 and was still going.

## What was running

| Item | Value |
|---|---|
| Process | `master_pipeline.py --analytics intruder`, PID 2437432 on main, started 05:51:59 |
| Env | `SHIFT_SESSION=1`, `SHIFT_ID=160`, `EXECUTION_ID=ses-42-160-2026-10-15` |
| Session file | `script-server/scripts/logs/sessions/ses-42-160-2026-10-15.json` |

`#12901` is only a **member** (one analytic window) of the session. The session is one process per (exam, shift, day).
The assignment status in the UI is written per window, but the process lives until the **whole session** is done.

## Root cause

How the session decides to stop (`master_pipeline.py` around line 1726 and `poller.py:189-196`):

- Session end = `max(end of every window in the session file, shift end)`.
- The loop exits only when `now > max(window ends) + SESSION_END_GRACE_S`.

The session file contained three intruder windows:

| # | Start | End | Source |
|---|---|---|---|
| 1 | 15 Oct 05:17 | 15 Oct 06:00 | earlier launch |
| 2 | **15 Oct 05:24** | **16 Oct 00:00** | **ghost window, no matching assignment in DB now** |
| 3 | 15 Oct 05:52 | 15 Oct 06:00 | #12901 (the real one) |

Window #2 kept the session alive until midnight. Two defects combine to cause this.

### Defect 1 — overnight rule turns a bad end time into "next day"

`cctv_backend/src/controllers/ExecutionController.js` (lines ~176 and ~288):

```js
if (endTimeStr && startTimeStr && endTimeStr <= startTimeStr) {
    // treat as overnight → end_at = next day
}
```

An assignment with start `05:24` and end `00:00:00` (midnight / "12:00 AM", easy to enter by mistake)
gets `end_at = 2026-10-16T00:00:00`. Any end time **≤** start time becomes "tomorrow", even when it was a mistake.

This is my best explanation for window #2. The assignment row itself no longer exists in the DB, so I could not read its original values.
The 05:24 start and the 16 Oct 00:00 end are exactly what this rule produces for an end of `00:00:00`.

### Defect 2 — poller never forgets a window once seen

`poller.py` `_session_merge` (line ~204):

```python
rec["members"].update(assignment.get("_session") or {})
```

`rec["members"]` only ever grows during the poller's life. If an assignment is later edited, deleted or recreated
(at 05:26–05:27 the assignments for shift 160 were recreated with new ids 12880–12907), its old window stays in `rec["members"]`.
It is rewritten into the session file on every poll, so the running pipeline keeps seeing it.
The pipeline re-reads that file and so never reaches "every window ended".

## Why it only showed up now

- On 14 Oct the session file had only correct windows, so the session ended on time.
- On 15 Oct assignments were being created and edited around 05:17–05:27 while a session for the same shift was already live.
- A wrong window got in and was never removed.
- The UI only shows per-assignment status, so nothing looked wrong.

## What was done (live)

- The main process (PID 2437432) was stopped with SIGTERM (exit code 0).
- The same session was still running on workers 2, 3, 4, 5. `POST /executions/ses-42-160-2026-10-15/cancel` was sent to each.
  Worker 3 had already exited by then.
- Afterwards `/executions/running` returned count 0 on all five workers.
- No code was changed.

## Proposed fixes (not applied)

### Fix A — poller: drop windows that are no longer in the DB (the main fix)

In `_session_merge`, replace the members of this session with the current poll's windows (for the ids still returned by the backend), instead of only adding.
Keep windows that already ended or already started only if the assignment is still `is_active` in the DB.

Sketch:

```python
current = assignment.get("_session") or {}
# keep only members still returned by the backend (or already finished, for status bookkeeping)
rec["members"] = {mid: m for mid, m in rec["members"].items()
                  if mid in current or rec["status"].get(mid) == "COMPLETED"}
rec["members"].update(current)
```

Caveat: the backend only returns PENDING assignments in a lookahead window, plus RUNNING ones.
A member that is currently RUNNING must still be returned, otherwise it would be dropped mid-run.
Verify what the poll returns for RUNNING members before using the filter above,
or have the poller ask the backend for the session's members by shift and date.

### Fix B — pipeline: cap a window's end at the shift end

In `master_pipeline.py`, ignore any window `end` that is later than the shift's end for that day
(`_shift_end` is already carried in the session record). A window after the shift end is never correct for the session.
This makes a stale or wrong window harmless, even if Fix A is missed.

### Fix C — backend: do not guess "overnight" for obviously bad ends

In `ExecutionController.js`, treat `end <= start` as overnight only when the shift itself is overnight
(for example `shift_end_time <= shift_start_time`).
Otherwise reject the assignment at save time in `ExamScheduleController.js` (the validation that creates assignments) with a clear error.
Also validate in the UI (`ScheduleSections.jsx`) that end > start.

### Fix D — operational safety net

Add a hard limit: the pipeline exits when `now > shift_end + SESSION_END_GRACE_S`, regardless of windows.
Log a warning with the window id that is keeping the session alive.

## Recommended order

1. A + B (small, safe, in poller and pipeline).
2. C (backend and UI validation).
3. D as a backstop.

## Deployment notes

- Workers w1–w5 have code baked in the image: deploy by `docker cp` + `docker restart`, never recreate (see memory: worker code baked in image).
- `poller.py` on workers carries new_dev-only lease code. Patch surgically, never overwrite from main.
- Test on main (worker-0) first: create an assignment with start 05:24 / end 00:00, then edit it,
  and confirm the session file no longer holds the stale window and the process exits at the real end.

## How to verify the cause on another day

```bash
# windows the running session is holding
python3 -c "import json,datetime as d;j=json.load(open('script-server/scripts/logs/sessions/ses-42-160-<date>.json'))
[print(a,[d.datetime.fromtimestamp(w[k]).strftime('%d %H:%M') for k in ('start','end')]) for a,v in j['analytics'].items() for w in v['windows']]"
# running on each worker
curl -s http://<worker>:8000/executions/running
```
