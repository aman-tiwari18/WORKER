# Exam schedule, centre schedule, server-room thresholds and the RTSP classifier changes

What was built, how it works, and what changed in the backend, worker and frontend.
All of it lives only in the new setup (`/data/new_dev`: backend, frontend, worker).

---

## 1. Summary

| Feature | What it does | Where |
|---|---|---|
| **Exam date & shift schedule** | Upload the "Exam Schedule" sheet (Date × Shift 1/2/3, Yes/No). The exam runs only in the slots marked Yes. The same sheet can also **fill the exam form** (dates, skipped dates, shifts). | Exams → Add/Edit → "Import schedule from Excel" and "Exam date & shift schedule" |
| **Centre-wise schedule (RTSP)** | Upload the "Exam Centre wise scheduling" sheet (TC code × "date Shift n", YES/NO). In each date and shift, only the centres marked YES load cameras, so there is no detection, classification or alert anywhere else. A matrix lets you view and edit it. | Exams → Edit → "Centre-wise schedule (RTSP)" |
| **Server-room thresholds (HLS + RTSP)** | Upload the "Server Room Threshold" sheet per exam, instead of editing `server_room_constants.py` and redeploying. You can view, edit and export it. | Exams → Edit → "Server room thresholds" |
| **RTSP classifier** | Classify only cameras whose label is empty or "Uncertain…". Never relabel a camera a person verified. Start vLLM only when something needs it. Reset manual verification only when the label really changes. | `worker/scripts/detection/vision_classification_rtsp.py`, `utils/db_client.py`, backend gateway |

No uploaded sheet means the old behaviour: everything runs, and the legacy threshold constants apply. Existing exams are unaffected until someone uploads.

---

## 2. Rules (how decisions are made)

### Which RTSP cameras run in a date + shift

A camera runs only if both of these hold:

1. **Exam level** (`exam_slot_schedule`): the exam has no slot rows, **or** this date + shift is listed with `is_running = 1`. A date missing from the sheet does not run.
2. **Centre level** (`exam_centre_schedule`): the exam has no centre rows, **or** the camera's `tc_code` is listed for this date + shift with `is_running = 1`. A centre with cameras that is missing from the sheet does not run.

An exam-level "No" always wins over centre-level YES cells.

Where it is enforced:
- the worker's RTSP camera queries, for **detection and classification**, in both DB access modes:
  - `DB_ACCESS_MODE=direct`: SQL in `worker/.../utils/db_client.py` (`_rtsp_schedule_sql`, `_fetch_rtsp`);
  - `DB_ACCESS_MODE=backend`: SQL in `backend/src/controllers/WorkerDbController.js` (`rtspScheduleClause` in `src/utils/examSchedule.js`).
- The date and shift come from the run's launch env (`ASSIGNMENT_DATE`, `SHIFT_ID`), which `app/script_runner.py` already sets. No analytic script changed.
- The filter runs **before** the worker's hash/lease split, so workers divide only the cameras that run.
- If the tables are missing (migration not applied), the filter is skipped and the old behaviour continues. A missing migration never stops detection.
- The pipeline loads its camera list at launch, so an edit takes effect on the next launch of that shift's job.

**Reports:** the per-shift RTSP camera lists (`ReportController.rtspShiftCameraLists`, used by all three report paths) leave out cameras of centres that do not run in that shift. They count as "not scheduled", not "offline".

### Server-room threshold lookup (`get_min_persons`)

Most specific first:
1. The exam's **uploaded** sheet, for this exact sitting (date + shift id from the launch env).
2. The exam's uploaded sheet, exam-wide row ("every sitting").
3. If the exam has an uploaded sheet but this centre is not in it: **`None` → no server-room detection there.** The value is never guessed or borrowed from another exam.
4. Only if the exam has **no** uploaded sheet: the legacy tables in `server_room_constants.py`, exactly as before.
   - `SERVER_ROOM_LEGACY_FALLBACK=0` switches the legacy tables off.

Detection fires when `person_count > limit`. The sheet's fractional limits (11.01) are **rounded down** (11), which means the same thing for whole people and matches the old constants convention.

Workers cache an exam's thresholds for `SERVER_ROOM_THRESHOLD_TTL_S` (default 300 s). If the DB or backend cannot be reached, the last good copy keeps being used. With no copy, the lookup falls back to the legacy tables. It never raises.

### RTSP classifier: which cameras go to vLLM

A camera goes to vLLM only if all of these hold:
- it is reachable in Pass-1;
- it is **not** manually verified;
- its label is empty or starts with "uncertain" ("Uncertain", "Uncertain/Obstructed").

`CLASSIFY_ONLY_UNLABELED=0` classifies every reachable camera again.

---

## 3. Database (migration `046_exam_schedule_and_thresholds.sql`)

| Table | Purpose | Key |
|---|---|---|
| `exam_slot_schedule` | exam-level Yes/No per date + shift | PK `(exam_id, exam_date, shift_id)` |
| `exam_centre_schedule` | centre-level YES/NO per date + shift (`source` excel/manual) | UNIQUE `(exam_id, tc_code, exam_date, shift_id)` |
| `exam_centre_info` | centre name / region / city / state from the sheet (display only) | PK `(exam_id, tc_code)` |
| `server_room_thresholds` | limit per TC code; `exam_date`/`shift_id` NULL = every sitting | UNIQUE `(exam_id, tc_code, sitting_key)` (generated) |
| `exam_schedule_uploads` | audit: who uploaded which file, counts, warnings | |

- All tables have `ON DELETE CASCADE` to `exams`.
- `shift_id` is `exam_shifts.id`.
- In a sheet, "Shift N" maps to the exam's shift whose name contains N, else to its N-th shift by start time.

The migration is `CREATE TABLE IF NOT EXISTS`, so it is safe to run again. Apply only this file on production (`run.js` re-runs every migration):

```bash
mysql cctv_v2 < src/migrations/046_exam_schedule_and_thresholds.sql
```

---

## 4. API (all under `<base>/api`)

Writes are **ADMIN** only; reads are ADMIN/OPERATOR. Errors are always JSON: `{ success:false, message, errors?[], warnings?[] }`.

| Method & path | What |
|---|---|
| `POST /exams/schedule-sheet/parse` | read an "Exam Schedule" sheet → values for the exam form (exam need not exist) |
| `POST /exams/:id/slot-schedule/upload[?dry_run=1]` | exam-level schedule (preview with `dry_run=1`) |
| `GET / DELETE /exams/:id/slot-schedule` | read / clear |
| `POST /exams/:id/centre-schedule/upload[?dry_run=1]` | centre schedule (**RTSP exams only**) |
| `GET /exams/:id/centre-schedule` | matrix: centres × slots, per-slot totals, camera counts |
| `PUT /exams/:id/centre-schedule/cell` | one cell `{tc_code,date,shift_id,is_running}` or a whole slot `{date,shift_id,is_running,all:true}` |
| `GET /exams/:id/centre-schedule/export` | Excel in the upload format (can be re-uploaded as is) |
| `DELETE /exams/:id/centre-schedule` | clear |
| `GET /exams/:id/slot-summary?date=&shift_id=` | does the exam run, how many centres/cameras |
| `POST /exams/:id/server-room-thresholds/upload[?dry_run=1]` | thresholds (flat or per-sitting sheet) |
| `GET /exams/:id/server-room-thresholds?search=&limit=&offset=` | list + centres with server-room cameras but no threshold |
| `PUT /exams/:id/server-room-thresholds` | upsert one `{tc_code,max_persons,exam_date?,shift_id?}` |
| `DELETE /exams/:id/server-room-thresholds[/:rowId]` | clear all / one row |
| `GET /exams/:id/server-room-thresholds/export` | Excel |
| `GET /exams/{centre-schedule,slot-schedule,server-room-thresholds}/template` | blank templates |
| `GET /worker-db/server-room-thresholds?exam_id=` | worker read (worker key) |
| `GET /worker-db/cameras?link_type=rtsp&exam_id=&exam_date=&shift_id=` | now applies the schedule; also returns `manually_verified` |

Every upload is checked and replaced in one transaction:
- with errors → **422** and nothing is written;
- `dry_run=1` → the same checks, nothing written.

---

## 5. Excel formats

**Exam Schedule** (exam level):

| Sr.No. | Date | Shift 1 (10:00:00 to 11:00:00) | Shift 2 (13:30:00 to 14:30:00) | … |
|---|---|---|---|---|
| 1 | 30-Sep-26 | No | Yes | |

- Dates as `30-Sep-26`, `30-09-2026`, `2026-09-30` or Excel dates. Cells Yes/No.
- Times before 06:00 without AM/PM (e.g. `01:30`) produce a warning, because they may mean 13:30.

**Centre wise scheduling** (RTSP):

| TC CODE | CENTRE NAME | REGION | ACTUAL CITY | ACTUAL STATE | … | 30-9-26 Shift 1 | 30-9-26 Shift 2 | … |
|---|---|---|---|---|---|---|---|---|
| 50106 | … | CR | … | … | | NO | YES | |

- Headers like `d-m-yy Shift n`; extra spaces are tolerated.
- TC codes are matched as text: `50106.0` becomes `50106`, and `351603_1` stays as is.
- Other columns are informational.

**Server Room Threshold**:
- **flat** (CGLE): `TC CODE | CENTRE NAME | REGION | Exam Nodes | Minimum Required | Additional IT Manager | Maximum Server Room Threshold`.
- **per sitting** (UGC/UPCISB style): `TC CODE | CENTRE NAME | <d-m-yy Shift n> …` holding the limit, where 0 or empty means not scheduled.

---

## 6. Errors the user sees (examples)

| Situation | HTTP | Message shown |
|---|---|---|
| no file / wrong field | 400 | "No file uploaded (send the Excel file in the "file" field)" |
| not an Excel file | 400 | "Only Excel files (.xls, .xlsx) are allowed" |
| over 10 MB | 400 | "The file is too large (max 10 MB)" |
| corrupt / unreadable workbook | 400 | "The file could not be read as an Excel workbook (…)" |
| wrong sheet shape | 400 | "No "TC CODE" column found. The first sheet needs …" |
| HLS exam + centre schedule | 400 | "The centre schedule applies to RTSP exams only; "X" is HLS." |
| bad cell / duplicate TC / missing shift | 422 (on save) | listed one per line: `Row 2 (50106), 5-10-26 Shift 2: "MAYBE" is not YES or NO`, `The exam has no shift for: Shift 9. …` |
| exam missing | 404 | "Exam not found" |
| migration not applied | 503 | "The schedule tables are missing — run migration 046 …" |
| edit before any upload | 409 | "Upload a centre schedule first — there is nothing to edit yet." |

Warnings never block saving. They cover:
- centres with cameras but not in the sheet;
- TC codes without cameras;
- slots where no centre runs;
- dates outside the exam period;
- disagreement with the exam-level sheet;
- centres with server-room cameras but no threshold.

The frontend shows everything in the preview dialog. Confirm stays disabled while there are errors. Network failures, timeouts and permission errors get plain-language messages (`describeError` in `ScheduleSections.jsx`).

---

## 7. What changed, file by file

**Backend**
- `src/migrations/046_exam_schedule_and_thresholds.sql` — new tables.
- `src/utils/examSchedule.js` — Excel parsers, shift mapping, enforcement SQL (`rtspScheduleClause`), `runningScope`.
- `src/controllers/ExamScheduleController.js` — slot + centre schedule endpoints, export, templates, form parse.
- `src/controllers/ServerRoomThresholdController.js` — threshold endpoints.
- `src/routes/exams.js` — routes, plus clear 400s for upload errors (`uploadSheet`).
- `src/controllers/WorkerDbController.js` and `src/routes/workerDb.js`:
  - the schedule filter on RTSP camera reads, plus `manually_verified`;
  - `update_rtsp` keeps manual verification unless the label changes;
  - the new `/worker-db/server-room-thresholds`.
- `src/controllers/ReportController.js` — per-shift RTSP lists respect the schedule.

**Worker**
- `scripts/detection/utils/db_client.py`:
  - RTSP camera reads apply the schedule (`_run_slot`, `_rtsp_schedule_sql`, `_fetch_rtsp`);
  - `get_rtsp_cameras` returns `manually_verified`;
  - `update_rtsp_classification` resets verification only on a real label change (the `IF()`s come before `classification = %s`; MySQL applies SET left to right).
- `scripts/detection/utils/db_gateway.py` — sends `exam_date` + `shift_id` with RTSP camera reads.
- `scripts/detection/server_room_thresholds_db.py` (new) — cached threshold lookup (backend or DB).
- `scripts/detection/server_room_constants.py` — `get_min_persons` asks the uploaded sheet first. New switch `SERVER_ROOM_LEGACY_FALLBACK`.
- `scripts/detection/vision_classification_rtsp.py`:
  - `CLASSIFY_ONLY_UNLABELED` (default 1) and `_needs_classification()`.
  - vLLM starts **after Pass-1** and only if a reachable camera needs a label. RTSP assignment runs only; HLS and standalone mode are unchanged. A worker with nothing to label never loads vLLM.
  - Pass-2 runs only on cameras that need a label; the "Pass-1 ok, Pass-2 failed" revert applies to those only.
  - Cameras that keep their label get a `camera_classification_rtsp` history row (existing label, shift, date, frame), so the per-shift report and the run picker still see them.
  - Shutdown and SIGTERM cope with vLLM never having started, and a run that never touched vLLM does not stop someone else's.

**Frontend**
- `src/pages/exams/ScheduleSections.jsx` (new):
  - the three sections;
  - the upload preview (validate → confirm);
  - the centre matrix (click a cell to toggle, click a column count to set all, search, region filter, manual-change markers);
  - the threshold editor (search, inline edit, add one, delete);
  - downloads and error handling.
- `src/pages/Exams.jsx`:
  - "Import schedule from Excel" fills dates, skipped dates and shifts. On an existing exam its shifts are kept and only missing ones are added; differences are listed.
  - The same file is stored as the exam's date/shift schedule on Save.
  - The sections sit below the SMS observer numbers.
- `src/services/api.js` — the new `examsAPI` calls.

---

## 8. Settings (env)

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `CLASSIFY_ONLY_UNLABELED` | worker | `1` | `0` = classify every reachable RTSP camera again |
| `SERVER_ROOM_LEGACY_FALLBACK` | worker | `1` | `0` = exams without an uploaded sheet get no server-room alerts (constants file unused) |
| `SERVER_ROOM_THRESHOLD_TTL_S` | worker | `300` | how long a worker caches an exam's thresholds |
| `FRAMES_DIR`, `MAX_FILE_SIZE`, `UPLOAD_DIR` | backend | existing | unchanged |

---

## 9. Deploy

1. Apply `046_exam_schedule_and_thresholds.sql` to the database (production and dev).
2. Deploy the backend, then the frontend, then the worker. The order is safe either way:
   - an old worker ignores the new tables;
   - a new worker without the tables skips the filter;
   - the backend answers "no thresholds" if its table is missing.
3. Restart workers outside classification windows. The classifier change takes effect on the next classification run.

**Roll back:** clear the exam's sheets from the Exams page (or `DELETE` the rows). The old behaviour returns immediately, with no redeploy.

---

## 10. Tested

- **Backend API (74 checks), against a throwaway MySQL with the production schema and the real sheets:**
  - "Exam Centre wise scheduling_ CDIS.xlsx": 43 centres × 54 slots.
  - "Server Room Threshold_CGLE 2026.xlsx": 221 centres.
  - Covered: permissions, every upload error, preview vs save, matrix totals (30-Sep Shift 1 = 0 centres, Shift 2 = 34), the cameras workers get per slot, cell and column edits, export round trip, exam-level schedule and form fill, flat and per-sitting thresholds on RTSP and HLS exams, and the verification-preserving update.
- **Worker in the real image:**
  - direct-DB mode and backend mode return the same camera sets (66 / 0 / 87);
  - threshold lookup covers sitting, then exam-wide, then missing → None, then legacy fallback, the switch off, and DB unreachable;
  - classifier flows: uncertain/empty → vLLM; verified and labelled kept with history rows; nothing to label → vLLM never started; switch off → all; shutdown without vLLM.
- **UI in a browser (Playwright):** sections, matrix, threshold editor, error preview (Confirm disabled), wrong-file message, form import. No page errors.

---

## 11. Counts on the dashboards, the client API and Schedule Analysis

For RTSP exams with an uploaded schedule, wherever a page shows a **specific date or shift** the camera count includes only the centres scheduled then. Exam-wide totals with no date stay the full camera inventory.

| Where | Change |
|---|---|
| Admin and client dashboard **overview** (`/dashboard/overview`, `/client-dashboard/overview`) | single day: cameras of centres scheduled that day (only the chosen shifts, if a shift filter is set). Date ranges keep the full inventory. |
| Admin and client **live exams** table | each (exam, shift) row counts the cameras of the centres running in that shift today |
| Client **camera accessibility** list + download | with `exam_date` (and `shift_id`), RTSP cameras are limited to centres scheduled then |
| **Client API** `/api/v1/cameras` (and the camera-type master) | a camera whose centre has no exam right now (the current shift, or today when between shifts) is reported **Idle/Ready**, not live or "Other" |
| **Schedule Analysis** page | chip under the shift tabs: "Shift 2 on 05 Oct: 33 centres running · 66 of 87 cameras", or "exam not running" (red). Uses `/exams/:id/slot-summary`. |

Shared helpers are in `src/utils/examSchedule.js`: `scheduledExamIds`, `rtspScheduledCounts`, `rtspScheduledCountsPerShift`, `rtspRunsOnDateSql`, `scheduledTcCodesNow`. Exams without a schedule are untouched, and so is everything when migration 046 is missing.

## 12. Decisions

- **HLS classifier:** `update_hls_classification` still resets manual verification on every write. Only RTSP was changed, as requested.
- **Uncertain verified by hand:** a camera a person verified as "Uncertain/Obstructed" stays skipped, because a person's decision wins.
- **Mid-shift edits** apply on the next launch of that shift's job.
- **Existing per-exam constants** (UGC re-exam, UPCISB, CGLE) keep working through the legacy fallback until their sheets are uploaded on the new setup's exams.
