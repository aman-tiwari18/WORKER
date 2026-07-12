import os
import re
import json
import asyncio
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

from app.config import SCRIPTS_DIR, MAX_CONCURRENT_SCRIPTS
from app.logger import logger
from app.log_manager import log_manager

IST = timezone(timedelta(hours=5, minutes=30))

# Named script log files: scripts/logs/{ScriptName}_{Exam}_{Shift}_{Date}.log
SCRIPT_LOGS_DIR = Path(SCRIPTS_DIR).resolve() / "logs"
SCRIPT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT_TERMINATE_GRACE_SECONDS = float(os.getenv("SCRIPT_TERMINATE_GRACE_SECONDS", "20"))


def _sanitize_log_component(value: str, fallback: str) -> str:
    """
    Make a script/exam/shift name safe for use inside a filename.

    Strips whitespace AND any character that is not alphanumeric, dot, or
    hyphen — in particular path separators like '/' which appear in names such
    as "Crowd Detection (Entry/Exit)" and would otherwise be treated as a
    directory boundary, raising FileNotFoundError when the log file is opened.
    """
    cleaned = re.sub(r'[^A-Za-z0-9.\-]', '', str(value or ''))
    return cleaned or fallback


def _get_named_log_path(execution: dict) -> Path:
    """
    Build a log file path like:
      scripts/logs/ProximityDetection_JNTU_Morning_20260318.log
    """
    meta        = execution.get("execution_metadata", {})
    script_name = _sanitize_log_component(meta.get("script_name"), "Script")
    exam_name   = _sanitize_log_component(meta.get("exam_name"),   "Exam")
    shift_name  = _sanitize_log_component(meta.get("shift_name"),  "Shift")
    date_str    = datetime.now(IST).strftime("%Y%m%d")
    filename    = f"{script_name}_{exam_name}_{shift_name}_{date_str}.log"
    return SCRIPT_LOGS_DIR / filename


def _compute_timeout(execution: dict) -> float | None:
    """
    Calculate seconds until end_at (ISO datetime from backend),
    falling back to duration_minutes if end_at is absent.

    Backend always sends end_at as naive IST. We attach IST explicitly so
    all workers compute the same timeout regardless of their OS timezone.

    Handles overnight shifts: if end_at is in the past but within 24 h,
    it was an overnight assignment whose end is on the next day.
    Returns None if no timeout should be applied.
    """
    end_at_str = execution.get("end_at")
    if end_at_str:
        try:
            end_at = datetime.fromisoformat(end_at_str)
            # Attach IST if naive — backend always sends IST naive strings
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=IST)

            now = datetime.now(IST)
            remaining = (end_at - now).total_seconds()

            if remaining > 0:
                return remaining

            # end_at is in the past. Two very different cases:
            #  • GENUINE overnight shift: end_at wrapped past midnight, so it lands
            #    MANY hours behind us (≈ 24h − shift_length, e.g. ~21h for a 3h
            #    shift). Add one day to get the real next-day stop time.
            #  • Same-day window that simply CLOSED: the worker started this pipeline
            #    a little AFTER end_at (poll latency / grace catch-up), so end_at is
            #    only seconds-to-minutes behind us. This must NOT be inflated by a
            #    full day — that bug gave runaway pipelines a ~24h stop-timer and
            #    kept them detecting for hours after the shift ended (e.g. a grouped
            #    run that started at 18:00:21 ran until the next day).
            #
            # Require the gap to clearly exceed any same-day grace/latency (12h) and
            # stay within a day before treating it as overnight.
            OVERNIGHT_MIN_PAST_S = 12 * 3600
            if -86400 < remaining < -OVERNIGHT_MIN_PAST_S:
                overnight_end = end_at + timedelta(days=1)
                remaining_overnight = (overnight_end - datetime.now(IST)).total_seconds()
                if remaining_overnight > 0:
                    logger.info(
                        f"Overnight shift detected: end_at {end_at_str} was "
                        f"{(-remaining)/3600:.1f}h in the past — treating as next-day "
                        f"end at {overnight_end.isoformat()}"
                    )
                    return remaining_overnight

            # Window already closed (just ended) — do NOT run a fresh long pipeline.
            # Return a tiny timeout so the runner stops it immediately instead of
            # leaving it ungoverned (None) for ~24h.
            logger.warning(
                f"end_at {end_at_str} already passed ({(-remaining):.0f}s ago) — "
                f"shift window closed; stopping run immediately (no 24h extension)"
            )
            return 1.0
        except Exception as e:
            logger.warning(f"Could not parse end_at '{end_at_str}': {e}")

    # Fallback: use duration_minutes from the DB default
    duration_minutes = execution.get("duration_minutes")
    if duration_minutes:
        return duration_minutes * 60.0

    return None


class ScriptRunner:
    def __init__(self):
        self.scripts_dir = Path(SCRIPTS_DIR).resolve()
        self.max_concurrent = MAX_CONCURRENT_SCRIPTS
        self.running_count = 0
        # execution_id -> asyncio.subprocess.Process
        self._active_processes: dict = {}

    # Canonical script IDs accepted by the scheduler payload.
    SCRIPT_CODE_ALIASES = {
        "camera_tampering": "tampering",
        "tamper": "tampering",
        "camera_inversion": "inversion",
        "scene_classification": "vision",
        "blindspot": "blind_spot",
        "blind-spot": "blind_spot",
        "crowd_zero_det": "crowd_zero",
        "zero_person": "crowd_zero",
        "zero-person": "crowd_zero",
        "crowd_five_det": "crowd_five",
        "less_than_5": "crowd_five",
        "crowd_less_than_5": "crowd_five",
    }

    def _normalize_script_code(self, raw_script_code) -> str:
        script_code = str(raw_script_code or "").strip().lower()
        return self.SCRIPT_CODE_ALIASES.get(script_code, script_code)
    async def execute_script(self, execution):
        execution_metadata = execution.get("execution_metadata", {})
        raw_script_code = execution_metadata.get("script_code") or execution.get("script_code")
        script_code = self._normalize_script_code(raw_script_code)
        stream_url  = execution.get("stream_url") or execution_metadata.get("stream_url")
        camera_id   = execution.get("camera_id") or execution_metadata.get("camera_id")
        execution_id = str(execution.get("id"))

        pipeline_codes = self.PIPELINE_CODES

        link_type_for_path = str(execution_metadata.get("link_type") or execution.get("link_type") or "HLS").upper()

        if script_code == "vision":
            if link_type_for_path == "RTSP":
                full_script_path = self.scripts_dir / "detection/vision_classification_rtsp.py"
            else:
                full_script_path = self.scripts_dir / "detection/vision_classification.py"
        elif script_code == "vision_neet":
            # NEET scene classification + OCR — DB-driven, shared vLLM backend.
            # Handles HLS/RTSP internally via the assignment payload's link_type.
            full_script_path = self.scripts_dir / "detection/vision_classification_neet.py"
        elif script_code in pipeline_codes:
            full_script_path = self.scripts_dir / "detection/pipeline/master_pipeline.py"
        else:
            raise ValueError(
                f"Unknown script code: {raw_script_code!r} (normalized={script_code!r})"
            )

        if not full_script_path.exists():
            raise FileNotFoundError(f"Script not found: {full_script_path}")

        # ──── Named log file ─────────────────────────────────────────────────────
        log_path = _get_named_log_path(execution)
        meta      = execution_metadata
        header = (
            f"\n{'='*70}\n"
            f"  Script   : {meta.get('script_name', script_code)}\n"
            f"  Exam     : {meta.get('exam_name', 'N/A')}\n"
            f"  Shift    : {meta.get('shift_name', 'N/A')}\n"
            f"  Started  : {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST\n"
        )
        end_at_str = execution.get("end_at")
        if end_at_str:
            header += f"  Stop at  : {end_at_str}\n"
        header += f"{'='*70}\n"

        # Set up file logger
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        named_log = logging.getLogger(f"exec.{execution_id}")
        named_log.setLevel(logging.DEBUG)
        named_log.addHandler(file_handler)
        named_log.propagate = False

        named_log.info(header)

        timeout_s = _compute_timeout(execution)
        if timeout_s:
            named_log.info(f"  Timeout  : {timeout_s:.0f}s ({timeout_s/60:.1f} min)")
        named_log.info(f"  Pipeline : {full_script_path.relative_to(self.scripts_dir)}")
        named_log.info(f"  Analytics: {script_code}\n")

        logger.info(
            f"Executing script: {full_script_path} "
            f"for execution {execution_id} with analytics={script_code}"
        )
        logger.info(f"Named log → {log_path}")
        if timeout_s:
            logger.info(
                f"Script will run until "
                f"{end_at_str or f'{timeout_s/60:.1f}min'}"
            )

        log_manager.write_log(execution_id, f"Starting: {full_script_path}", "INFO")

        args = self.build_script_arguments(execution)

        ext = full_script_path.suffix
        command = sys.executable
        if ext == ".sh":  command = "bash"
        elif ext == ".js": command = "node"

        env = os.environ.copy()
        env["CAMERA_ID"]    = str(camera_id)    if camera_id    else ""
        env["STREAM_URL"]   = str(stream_url)   if stream_url   else ""
        env["EXECUTION_ID"] = execution_id
        env["MAIN_BACKEND_URL"] = os.environ.get("MAIN_BACKEND_URL", "http://localhost:3000/cctv_analytics/api")
        env["API_KEY"] = os.environ.get("API_KEY", "")

        meta = execution_metadata
        env["EXAM_ID"] = str(meta.get("exam_id") or execution.get("exam_id") or "")
        env["SHIFT_ID"] = str(meta.get("shift_id") or execution.get("shift_id") or "")
        env["ASSIGNMENT_DATE"] = str(meta.get("assignment_date") or execution.get("assignment_date") or "")
        env["LINK_TYPE"] = str(meta.get("link_type") or execution.get("link_type") or "")
        env["CAMERA_NAME"] = str(meta.get("camera_name") or execution.get("camera_name") or "")
        env["CAMERA_CLASSIFICATION"] = str(meta.get("classification") or execution.get("classification") or "")

        # Pass custom frequency as COOLDOWN_TIME (seconds) so workers override their default.
        freq_secs = meta.get("custom_frequency_minutes") or execution.get("custom_frequency_minutes")
        if freq_secs:
            env["COOLDOWN_TIME"] = str(int(freq_secs))

        # Both the regular vision classification (vision → rtsp/hls script) and the
        # NEET classification (vision_neet) consume the assignment via this payload.
        # vision_neet was previously excluded, so the NEET script launched with no
        # payload and silently fell back to its main() defaults (exam 2 / shift 7 /
        # HLS) — finding 0 cameras instead of the real RTSP exam.
        if script_code in ("vision", "vision_neet"):
            import json as _json
            env["VISION_ASSIGNMENT_PAYLOAD"] = _json.dumps({
                "assignment_id": execution_id,
                "exam_id": meta.get("exam_id") or execution.get("exam_id"),
                "shift_id": meta.get("shift_id") or execution.get("shift_id"),
                "assignment_date": meta.get("assignment_date") or execution.get("assignment_date"),
                "link_type": meta.get("link_type") or execution.get("link_type"),
            })

        process = await asyncio.create_subprocess_exec(
            command, str(full_script_path), *args,
            cwd=str(self.scripts_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True
        )

        # Register so cancel() can reach this process. For a grouped detection run
        # (one process serving many analytics) also register under every member
        # execution_id so a per-analytic cancel from the UI stops the shared process.
        self._active_processes[execution_id] = process
        for _mid in execution.get("_member_ids", []):
            if str(_mid) != execution_id:
                self._active_processes[str(_mid)] = process

        async def read_stream(stream, is_stderr=False):
            """Read output line by line, writing to both named log and log_manager."""
            data_chunks = []
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                data_chunks.append(text)
                if text:
                    named_log.info(f"{'STDERR' if is_stderr else 'STDOUT'} | {text}")
                    if is_stderr:
                        logger.warning(f"Script {execution_id} stderr: {text}")
                        log_manager.write_log(execution_id, text, "ERROR")
                    else:
                        logger.debug(f"Script {execution_id} stdout: {text}")
                        log_manager.write_log(execution_id, text, "INFO")
            return "\n".join(data_chunks)

        stdout_result = stderr_result = ""
        try:
            if timeout_s:
                res = await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(process.stdout, False),
                        read_stream(process.stderr, True)
                    ),
                    timeout=timeout_s
                )
                stdout_result, stderr_result = res
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            else:
                res = await asyncio.gather(
                    read_stream(process.stdout, False),
                    read_stream(process.stderr, True)
                )
                stdout_result, stderr_result = res
                await process.wait()

        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                logger.info(
                    f"⏹ Assignment {execution_id}: timeout reached; "
                    "SIGTERM sent to process group"
                )
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            try:
                await asyncio.wait_for(process.wait(), timeout=SCRIPT_TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Assignment {execution_id} did not exit after "
                    f"{SCRIPT_TERMINATE_GRACE_SECONDS:.0f}s; escalating to SIGKILL"
                )
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.error(f"Assignment {execution_id} did not terminate after SIGKILL")

            msg = (
                f"Script ran until end_at {end_at_str} — marking COMPLETED"
                if end_at_str else
                f"Script ran for {timeout_s/60:.1f}min — marking COMPLETED"
            )
            logger.info(f"⏹ Assignment {execution_id}: {msg}")
            named_log.info(f"\n{'='*70}\n  {msg}\n  Stopped at: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST\n{'='*70}\n")
            file_handler.close()
            named_log.removeHandler(file_handler)
            raise Exception("__TIMEOUT_COMPLETED__")

        finally:
            # Deregister and close file handler in all exit paths
            self._active_processes.pop(execution_id, None)
            for _mid in execution.get("_member_ids", []):
                self._active_processes.pop(str(_mid), None)
            try:
                file_handler.close()
                named_log.removeHandler(file_handler)
            except Exception:
                pass

        if process.returncode == 0:
            logger.info(f"Script {execution_id} completed successfully")
            named_log.info(f"\n{'='*70}\n  COMPLETED (exit code 0)\n  Finished at: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST\n{'='*70}\n")
            log_manager.write_log(execution_id, f"Script completed (exit code 0)", "INFO")
            out_str = str(stdout_result).strip() if stdout_result else ""
            try:
                result = json.loads(out_str)
            except Exception:
                result = {"output": out_str}
            return result
        else:
            logger.error(f"Script {execution_id} exited with code {process.returncode}")
            named_log.error(f"\n{'='*70}\n  FAILED (exit code {process.returncode})\n{'='*70}\n")
            log_manager.write_log(execution_id, f"Script FAILED (exit code {process.returncode})", "ERROR")
            raise Exception(f"Script exited with code {process.returncode}: {stderr_result}")

    # Detection analytics that run via master_pipeline.py. Used to (a) pick the
    # script path and (b) let the poller group all of a shift's detection analytics
    # into ONE master_pipeline process per worker (capture-once + shared YOLO).
    PIPELINE_CODES = {
        "server",
        "crowd",
        "crowd_five",
        "crowd_zero",
        "crowd_biometric",
        "intruder",
        "entry_exit",
        "proximity",
        "phone",
        "invigilator",
        "inversion",
        "tampering",
        "blind_spot",
        "strongroom",
        "qp_box",
    }

    # Script codes that are classification/vision passes (one-shot per worker
    # over its camera shard; must NOT auto-restart). Includes NEET classification.
    VISION_CODES = {"vision", "vision_neet"}
    # Maps script_code → analytics arg for master_pipeline.py (when key differs from script_code)
    ANALYTICS_ARG_OVERRIDES = {
        "tampering": "tamper",  # HLS_WORKER_MAP uses key "tamper"
    }

    def build_script_arguments(self, execution):
        args = []
        execution_metadata = execution.get("execution_metadata", {})
        raw_script_code = execution_metadata.get("script_code") or execution.get("script_code")
        script_code = self._normalize_script_code(raw_script_code)

        if script_code in self.VISION_CODES:
            # vision_classification.py receives assignment metadata as CLI args
            execution_id = str(execution.get("id", ""))
            exam_id      = str(execution_metadata.get("exam_id") or execution.get("exam_id") or "")
            shift_id     = str(execution_metadata.get("shift_id") or execution.get("shift_id") or "")
            link_type    = str(execution_metadata.get("link_type") or execution.get("link_type") or "HLS")
            assignment_date = str(execution_metadata.get("assignment_date") or execution.get("assignment_date") or "")
            args.extend([
                "--assignment_id", execution_id,
                "--exam_id",       exam_id,
                "--shift_id",      shift_id,
                "--link_type",     link_type,
                "--assignment_date", assignment_date,
            ])
        else:
            # master_pipeline.py uses --analytics <code...>. When the poller has
            # grouped a whole shift's detection analytics into one process, it sets
            # execution["_analytics"] = [all codes] so this worker captures each
            # camera ONCE and the unified detector fans out to every analytic.
            grouped = execution.get("_analytics")
            if grouped:
                codes = [self.ANALYTICS_ARG_OVERRIDES.get(c, c) for c in grouped]
                codes = [c for c in codes if c]
                if codes:
                    args.extend(["--analytics", *codes])
            else:
                analytics_arg = self.ANALYTICS_ARG_OVERRIDES.get(script_code, script_code)
                if analytics_arg:
                    args.extend(["--analytics", analytics_arg])

        return args

    async def cancel(self, execution_id: str) -> bool:
        """
        Terminate a running script process by execution_id, including all of its
        mp.Process workers and any descendants (torch DataLoader helpers, etc.)
        that hold GPU memory.

        Returns True if the process was found and kill was attempted, False if not found.
        """
        process = self._active_processes.get(str(execution_id))
        if process is None:
            return False

        # Capture pgid up front. Once master_pipeline exits, os.getpgid(pid) raises
        # ProcessLookupError — so we can't query it later when we need to clean up
        # workers that outlived the master.
        pgid = None
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            logger.info(f"Script {execution_id} already terminated")
            return False
        except Exception as e:
            logger.warning(f"Could not get pgid for {execution_id}: {e}")

        # 1. Graceful: SIGTERM to the whole process group.
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
                logger.info(f"⏹ {execution_id}: SIGTERM sent to pgid {pgid}")
            else:
                process.terminate()
                logger.info(f"⏹ {execution_id}: SIGTERM sent to main pid (no pgid)")
        except ProcessLookupError:
            return False
        except Exception as e:
            logger.warning(f"SIGTERM failed for {execution_id}: {e}")

        # 2. Wait for master_pipeline to exit.
        try:
            await asyncio.wait_for(process.wait(), timeout=SCRIPT_TERMINATE_GRACE_SECONDS)
            logger.info(f"⏹ {execution_id}: master pipeline exited after SIGTERM")
        except asyncio.TimeoutError:
            logger.warning(
                f"Script {execution_id} did not exit after SIGTERM in "
                f"{SCRIPT_TERMINATE_GRACE_SECONDS:.0f}s — escalating to SIGKILL"
            )

        # 3. ALWAYS SIGKILL the process group as a cleanup pass.
        # Even when master_pipeline exits cleanly, its mp.Process workers (which hold
        # GPU memory) can outlive it — they catch SIGTERM in their own code, get stuck
        # in CUDA calls, or spawn DataLoader helpers that get orphaned. They stay in
        # master's pgid (mp.Process with spawn doesn't call setsid), so killpg(SIGKILL)
        # reaches every survivor. Without this, GPU memory leaks until manual kill.
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
                logger.info(f"⏹ {execution_id}: SIGKILL sweep on pgid {pgid} (clears worker stragglers)")
            except ProcessLookupError:
                # Group already empty — happy path
                pass
            except Exception as e:
                logger.warning(f"SIGKILL sweep for {execution_id} failed: {e}")
        else:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        # 4. Best-effort wait so the GPU memory is actually freed before we return.
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.error(f"Script {execution_id} did not terminate after SIGKILL")
        return True

    def list_running(self) -> list[str]:
        """Return list of currently running execution IDs."""
        return list(self._active_processes.keys())


script_runner = ScriptRunner()