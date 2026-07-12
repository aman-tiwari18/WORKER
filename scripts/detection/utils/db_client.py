# """
# Database Client for CCTV Pipeline
# ===================================
# Connects to MySQL and queries camera_links_hls / camera_links_rtsp tables.
# """

# import os
# import logging
# import pymysql

# logger = logging.getLogger(__name__)

# # ================= DB CONFIG =================
# DB_CONFIG = {
#     "host":     os.environ.get("DB_HOST", "localhost"),
#     "port":     int(os.environ.get("DB_PORT", 3306)),
#     "user":     os.environ.get("DB_USER", "root"),
#     "password": os.environ.get("DB_PASSWORD", "rootpassword123"),
#     "database": os.environ.get("DB_NAME", "cctv_monitoring"),
#     "charset":  "utf8mb4",
#     "cursorclass": pymysql.cursors.DictCursor,
# }


# def _ensure_status_column(conn):
#     try:
#         with conn.cursor() as cur:
#             cur.execute("ALTER TABLE camera_links_hls ADD COLUMN status VARCHAR(255) DEFAULT NULL")
#         conn.commit()
#     except Exception:
#         pass
#     try:
#         with conn.cursor() as cur:
#             cur.execute("ALTER TABLE camera_links_rtsp ADD COLUMN status VARCHAR(255) DEFAULT NULL")
#         conn.commit()
#     except Exception:
#         pass

# def _get_connection():
#     """Create a new MySQL connection."""
#     conn = pymysql.connect(**DB_CONFIG)
#     if not getattr(_get_connection, 'migrated', False):
#         _ensure_status_column(conn)
#         _get_connection.migrated = True
#     return conn


# # ============================================================
# # 🎥  HLS CAMERAS
# # ============================================================
# def get_hls_cameras(exam_id: int, shift_id: int, exam_date: str) -> list:
#     """
#     Query camera_links_hls for cameras matching exam_id, shift_id,
#     and exam_date (YYYY-MM-DD).

#     Returns list of dicts with keys:
#         cctv_url, ch_code, classification, camera_name,
#         centre_name, city, state, tc_code, camera_category
#     """
#     sql = """
#         SELECT
#             id, exam_id, shift_id, centre_name, city, state,
#             camera_name, ch_code, tc_code, cctv_url,
#             camera_category, classification, status, is_active, exam_date
#         FROM camera_links_hls
#         WHERE exam_id   = %s
#           AND (shift_id = %s OR shift_id IS NULL)
#           AND (exam_date = %s OR exam_date IS NULL)
#           AND is_active = 1
#     """
#     params = [exam_id, shift_id, exam_date]

#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, tuple(params))
#                 rows = cur.fetchall()
#                 logger.info(
#                     f"[DB] Loaded {len(rows)} HLS cameras "
#                     f"(exam_id={exam_id}, shift_id={shift_id}, date={exam_date})"
#                 )
#                 return rows
#     except Exception as e:
#         logger.error(f"[DB] Error querying camera_links_hls: {e}")
#         return []


# def update_hls_classification(exam_id: int, shift_id: int, exam_date: str, ch_code: str, classification: str, status: str = None) -> int:
#     """Update classification and status in camera_links_hls for one camera."""
#     sql = """
#         UPDATE camera_links_hls
#         SET classification = %s, status = %s
#         WHERE exam_id = %s
#           AND ch_code = %s
#           AND is_active = 1
#     """
#     params = [classification, status, exam_id, ch_code]
#     if shift_id is not None:
#         sql += " AND (shift_id = %s OR shift_id IS NULL)"
#         params.append(shift_id)
#     if exam_date is not None:
#         sql += " AND (exam_date = %s OR exam_date IS NULL)"
#         params.append(exam_date)
        
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, tuple(params))
#                 conn.commit()
#                 return cur.rowcount
#     except Exception as e:
#         logger.error(f"[DB] Error updating camera_links_hls classification (ch_code={ch_code}): {e}")
#         return 0

# def update_hls_status(exam_id: int, shift_id: int, exam_date: str, ch_code: str, status: str) -> int:
#     """Update only status in camera_links_hls for one camera."""
#     sql = """
#         UPDATE camera_links_hls
#         SET status = %s
#         WHERE exam_id = %s
#           AND ch_code = %s
#           AND is_active = 1
#     """
#     params = [status, exam_id, ch_code]
#     if shift_id is not None:
#         sql += " AND (shift_id = %s OR shift_id IS NULL)"
#         params.append(shift_id)
#     if exam_date is not None:
#         sql += " AND (exam_date = %s OR exam_date IS NULL)"
#         params.append(exam_date)
        
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, tuple(params))
#                 conn.commit()
#                 return cur.rowcount
#     except Exception as e:
#         logger.error(f"[DB] Error updating camera_links_hls status (ch_code={ch_code}): {e}")
#         return 0


# # ============================================================
# # 📡  RTSP CAMERAS
# # ============================================================
# def get_rtsp_cameras(exam_id: int) -> list:
#     """
#     Query camera_links_rtsp for cameras matching exam_id.

#     Returns list of dicts with keys:
#         cctv_url, device_id, classification, camera_name,
#         centre_name, city, state, tc_code
#     """
#     sql = """
#         SELECT
#             id, device_id, exam_id, centre_name, tc_code,
#             camera_name, camera_no, city, state, cctv_url,
#             is_active, classification, status
#         FROM camera_links_rtsp
#         WHERE exam_id  = %s
#           AND is_active = 1
#     """
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, (exam_id,))
#                 rows = cur.fetchall()
#                 logger.info(
#                     f"[DB] Loaded {len(rows)} RTSP cameras "
#                     f"(exam_id={exam_id})"
#                 )
#                 return rows
#     except Exception as e:
#         logger.error(f"[DB] Error querying camera_links_rtsp: {e}")
#         return []

# def get_rtsp_camera_by_id(camera_id_pk: int) -> dict:
#     """Fetch one active RTSP camera row by its primary key ID."""
#     if not camera_id_pk:
#         return {}

#     sql = """
#         SELECT
#             id, device_id, exam_id, centre_name, tc_code,
#             camera_name, camera_no, city, state, cctv_url,
#             is_active, classification
#         FROM camera_links_rtsp
#         WHERE id = %s
#           AND is_active = 1
#         LIMIT 1
#     """
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, (camera_id_pk,))
#                 row = cur.fetchone()
#                 return row or {}
#     except Exception as e:
#         logger.error(f"[DB] Error fetching camera_links_rtsp by ID ({camera_id_pk}): {e}")
#         return {}

# def update_rtsp_classification(exam_id: int, device_id: str, classification: str, status: str = None) -> int:
#     """Update classification and status in camera_links_rtsp for one camera."""
#     sql = """
#         UPDATE camera_links_rtsp
#         SET classification = %s, status = %s
#         WHERE exam_id = %s
#           AND device_id = %s
#           AND is_active = 1
#     """
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, (classification, status, exam_id, device_id))
#                 conn.commit()
#                 return cur.rowcount
#     except Exception as e:
#         logger.error(f"[DB] Error updating camera_links_rtsp classification (device_id={device_id}): {e}")
#         return 0

# def update_rtsp_status(exam_id: int, device_id: str, status: str) -> int:
#     """Update only status in camera_links_rtsp for one camera."""
#     sql = """
#         UPDATE camera_links_rtsp
#         SET status = %s
#         WHERE exam_id = %s
#           AND device_id = %s
#           AND is_active = 1
#     """
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, (status, exam_id, device_id))
#                 conn.commit()
#                 return cur.rowcount
#     except Exception as e:
#         logger.error(f"[DB] Error updating camera_links_rtsp status (device_id={device_id}): {e}")
#         return 0


# def get_hls_camera_by_ch_code(ch_code: str, exam_date: str = None) -> dict:
#     """Fetch one active HLS camera row by ch_code, optionally preferring a specific exam_date."""
#     if not ch_code:
#         return {}

#     sql_with_date = """
#         SELECT
#             id, exam_id, shift_id, centre_name, city, state,
#             camera_name, ch_code, tc_code, cctv_url,
#             camera_category, classification, is_active, exam_date
#         FROM camera_links_hls
#         WHERE ch_code = %s
#           AND is_active = 1
#         ORDER BY
#           CASE WHEN exam_date = %s THEN 0 ELSE 1 END,
#           exam_date DESC,
#           id DESC
#         LIMIT 1
#     """

#     sql_without_date = """
#         SELECT
#             id, exam_id, shift_id, centre_name, city, state,
#             camera_name, ch_code, tc_code, cctv_url,
#             camera_category, classification, is_active, exam_date
#         FROM camera_links_hls
#         WHERE ch_code = %s
#           AND is_active = 1
#         ORDER BY exam_date DESC, id DESC
#         LIMIT 1
#     """

#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 if exam_date:
#                     cur.execute(sql_with_date, (ch_code, exam_date))
#                 else:
#                     cur.execute(sql_without_date, (ch_code,))
#                 row = cur.fetchone()
#                 if not row:
#                     return {}
#                 return row
#     except Exception as e:
#         logger.error(f"[DB] Error querying camera_links_hls by ch_code ({ch_code}): {e}")
#         return {}


# def get_rtsp_camera_by_device_and_ch(device_id: str, camera_no: str) -> dict:
#     """Fetch one active RTSP camera row by device_id and camera_no."""
#     if not device_id or not camera_no:
#         return {}

#     sql = """
#         SELECT
#             id, device_id, exam_id, centre_name, tc_code,
#             camera_name, camera_no, city, state, cctv_url,
#             is_active, classification
#         FROM camera_links_rtsp
#         WHERE device_id = %s
#           AND camera_no = %s
#           AND is_active = 1
#         LIMIT 1
#     """
#     try:
#         conn = _get_connection()
#         with conn:
#             with conn.cursor() as cur:
#                 cur.execute(sql, (device_id, camera_no))
#                 row = cur.fetchone()
#                 return row or {}
#     except Exception as e:
#         logger.error(f"[DB] Error fetching camera_links_rtsp by device/ch ({device_id}/{camera_no}): {e}")
#         return {}


# # ============================================================
# # 🧪  STANDALONE TEST
# # ============================================================
# if __name__ == "__main__":
#     logging.basicConfig(level=logging.INFO)
#     from datetime import date

#     today = date.today().isoformat()
#     print(f"\n=== Testing HLS cameras (exam_id=2, shift_id=7, date={today}) ===")
#     hls = get_hls_cameras(2, 7, today)
#     for cam in hls[:3]:
#         print(f"  ch_code={cam['ch_code']}  url={str(cam['cctv_url'])[:60]}...")

#     print(f"\n=== Testing RTSP cameras (exam_id=2) ===")
#     rtsp = get_rtsp_cameras(2)
#     for cam in rtsp[:3]:
#         print(f"  device_id={cam['device_id']}  url={str(cam['cctv_url'])[:60]}...")




# Changes code on 15-04-2026
"""
Database Client for CCTV Pipeline
===================================
Connects to MySQL and queries camera_links_hls / camera_links_rtsp tables.
Provides a centralized report_to_db_hls() used by all HLS detection scripts.
"""

import os
import re
import logging
import pymysql
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── GPU / Worker ID ──────────────────────────────────────────────────────────
# Returns the unique fleet-wide worker slot (0..N-1) that identifies the
# *exact* container that produced a detection. With multi-GPU boxes (where
# WORKER_ID is e.g. "worker-5-gpu3"), the trailing-digit regex would collide
# across boxes (worker-5-gpu0 and worker-6-gpu0 both produce 0). To avoid
# that, honor WORKER_INDEX env when set — that's the unique slot the camera
# distributor uses. Fall back to the regex for single-GPU workers that don't
# set WORKER_INDEX.
def _get_gpu_id() -> int:
    idx = os.environ.get("WORKER_INDEX")
    if idx is not None and idx.strip() != "":
        try:
            return int(idx)
        except ValueError:
            pass
    wid = os.environ.get("WORKER_ID", "main")
    if wid in ("main", "worker-0"):
        return 0
    m = re.search(r'(\d+)$', wid)
    return int(m.group(1)) if m else 0

_GPU_ID = _get_gpu_id()

# Base directory for detection results (one level up from this utils/ dir)
_DETECTION_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ── Frame-path canonicalization ──────────────────────────────────────────────
# Cameras are captured from multiple hosts:
#   • main server     → /data/cctv_dev/cctv/script-server/scripts/detection/results/classification_frames/…
#   • main (alt path) → /data/cctv_dev/cctv/script-server/results/classification_frames/…  (symlinked to NFS)
#   • worker (NFS)    → /app/scripts/detection/results/classification_frames/…             (bind-mounted to NFS)
#   • worker (LOCAL)  → /app/results/classification_frames/…                               (broken — not NFS)
#
# The first three resolve to the same physical file on NFS at
# /data/nfs/cctv_images/classification_frames/<suffix>. By normalising at the
# DB boundary, every row stores ONE canonical absolute path that the main
# server (UI, analytics) can read directly without per-row guesswork.
#
# Override via env if your NFS root is mounted elsewhere.
_NFS_FRAMES_ROOT = os.environ.get(
    "NFS_FRAMES_ROOT",
    "/data/nfs/cctv_images/classification_frames",
).rstrip("/")
_FRAMES_MARKER = "/classification_frames/"


def _canonicalize_frame_path(p: Optional[str]) -> Optional[str]:
    """Normalize a worker-side frame_path to the canonical NFS form.

    Idempotent — safe to call on an already-canonical path.
    Leaves the value untouched (None / empty / no marker) if it can't be
    rewritten safely.
    """
    if not p:
        return p
    idx = p.find(_FRAMES_MARKER)
    if idx == -1:
        return p
    suffix = p[idx + len(_FRAMES_MARKER):]
    return f"{_NFS_FRAMES_ROOT}/{suffix}"


def _embed_gpu_id_in_image(image_path: str, gpu_id: int) -> None:
    """Embed gpu_id into JPEG EXIF UserComment field."""
    try:
        from PIL import Image
        img = Image.open(image_path)
        exif = img.getexif()
        exif[0x9286] = f"gpu_id={gpu_id}"  # 0x9286 = UserComment
        img.save(image_path, exif=exif.tobytes())
        img.close()
    except Exception as e:
        logger.debug(f"EXIF embed skipped for {image_path}: {e}")

# ================= DB CONFIG =================
DB_CONFIG = {
    "host":     os.environ.get("DB_HOST", "localhost"),
    "port":     int(os.environ.get("DB_PORT", 3306)),
    "user":     os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "rootpassword123"),
    "database": os.environ.get("DB_NAME", "cctv_monitoring"),
    "charset":  "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


def _ensure_status_column(conn):
    migrations = [
        "ALTER TABLE camera_links_hls ADD COLUMN status VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE camera_links_rtsp ADD COLUMN status VARCHAR(255) DEFAULT NULL",
        "ALTER TABLE camera_links_rtsp ADD COLUMN frame_path TEXT DEFAULT NULL",
        # Per-run classification history tables (mirrors migration 024).
        # Created here as well so workers come up cleanly without a manual migration step.
        """CREATE TABLE IF NOT EXISTS camera_classification_hls (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camera_id INT NOT NULL,
            shift_id INT NULL,
            exam_date DATE NULL,
            frame_path TEXT NULL,
            classification VARCHAR(255) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_camera_id (camera_id),
            INDEX idx_shift_date (shift_id, exam_date),
            INDEX idx_created_at (created_at)
        )""",
        """CREATE TABLE IF NOT EXISTS camera_classification_rtsp (
            id INT AUTO_INCREMENT PRIMARY KEY,
            camera_id INT NOT NULL,
            shift_id INT NULL,
            exam_date DATE NULL,
            frame_path TEXT NULL,
            classification VARCHAR(255) NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_camera_id (camera_id),
            INDEX idx_shift_date (shift_id, exam_date),
            INDEX idx_created_at (created_at)
        )""",
    ]
    for sql in migrations:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except Exception:
            pass

def _get_connection():
    """Create a new MySQL connection."""
    conn = pymysql.connect(**DB_CONFIG)
    if not getattr(_get_connection, 'migrated', False):
        _ensure_status_column(conn)
        _get_connection.migrated = True
    return conn


# ============================================================
# 🎥  HLS CAMERAS
# ============================================================
def get_hls_cameras(exam_id: int, shift_id: int, exam_date: str) -> list:
    """
    Query camera_links_hls for cameras matching exam_id, shift_id,
    and exam_date (YYYY-MM-DD).
    Automatically filters cameras based on worker assignment for horizontal scaling.
    
    NOTE: This loads ALL cameras regardless of status - used by vision classification scripts.
    For detection workers, use get_hls_cameras_accessible() instead.

    Returns list of dicts with keys:
        cctv_url, ch_code, classification, camera_name,
        centre_name, city, state, tc_code, camera_category
    """
    # Uses idx_exam_id and idx_shift_id indexes for optimal filtering
    sql = """
        SELECT
            id, exam_id, shift_id, centre_name, city, state,
            camera_name, ch_code, tc_code, cctv_url,
            camera_category, classification, status, is_active, exam_date
        FROM camera_links_hls USE INDEX (idx_exam_id, idx_shift_id)
        WHERE exam_id   = %s
          AND (shift_id = %s OR shift_id IS NULL)
          AND (exam_date = %s OR exam_date IS NULL)
          AND is_active = 1
        ORDER BY id
    """
    params = [exam_id, shift_id, exam_date]

    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
                
                # Apply camera distribution filter for horizontal scaling
                try:
                    try:
                        from app.camera_distributor import filter_cameras
                    except ImportError:
                        # Add parent directory to path to find 'app' package when running as standalone script
                        import sys
                        from pathlib import Path
                        root_dir = str(Path(__file__).resolve().parents[3])
                        if root_dir not in sys.path:
                            sys.path.append(root_dir)
                        from app.camera_distributor import filter_cameras
                    
                    rows = filter_cameras(rows)
                except Exception as e:
                    logger.warning(f"Failed to apply camera distribution filter: {e}")
                    # If distributor not available, return all cameras (single-node mode)
                    pass
                
                logger.info(
                    f"[DB] Loaded {len(rows)} HLS cameras "
                    f"(exam_id={exam_id}, shift_id={shift_id}, date={exam_date})"
                )
                return rows
    except Exception as e:
        logger.error(f"[DB] Error querying camera_links_hls: {e}")
        return []


def get_hls_cameras_accessible(exam_id: int, shift_id: int, exam_date: str) -> list:
    """
    Query camera_links_hls for ACCESSIBLE cameras only (status = 'Accessible').
    Used by detection workers - no point analyzing unreachable streams.
    Automatically filters cameras based on worker assignment for horizontal scaling.

    Returns list of dicts with keys:
        cctv_url, ch_code, classification, camera_name,
        centre_name, city, state, tc_code, camera_category
    """
    # Uses idx_exam_id and idx_shift_id indexes for optimal filtering
    sql = """
        SELECT
            id, exam_id, shift_id, centre_name, city, state,
            camera_name, ch_code, tc_code, cctv_url,
            camera_category, classification, status, is_active, exam_date
        FROM camera_links_hls USE INDEX (idx_exam_id, idx_shift_id)
        WHERE exam_id   = %s
          AND (shift_id = %s OR shift_id IS NULL)
          AND (exam_date = %s OR exam_date IS NULL)
          AND is_active = 1
          AND status = 'Accessible'
        ORDER BY id
    """
    params = [exam_id, shift_id, exam_date]

    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
                
                # Apply camera distribution filter for horizontal scaling
                try:
                    try:
                        from app.camera_distributor import filter_cameras
                    except ImportError:
                        # Add parent directory to path to find 'app' package when running as standalone script
                        import sys
                        from pathlib import Path
                        root_dir = str(Path(__file__).resolve().parents[3])
                        if root_dir not in sys.path:
                            sys.path.append(root_dir)
                        from app.camera_distributor import filter_cameras
                    
                    rows = filter_cameras(rows)
                except Exception as e:
                    logger.warning(f"Failed to apply camera distribution filter: {e}")
                    # If distributor not available, return all cameras (single-node mode)
                    pass
                
                logger.info(
                    f"[DB] Loaded {len(rows)} ACCESSIBLE HLS cameras "
                    f"(exam_id={exam_id}, shift_id={shift_id}, date={exam_date})"
                )
                return rows
    except Exception as e:
        logger.error(f"[DB] Error querying camera_links_hls (accessible): {e}")
        return []


def update_hls_classification(exam_id: int, shift_id: int, exam_date: str, ch_code: str, classification: str, status: str = None, frame_path: str = None) -> int:
    """Update classification, status, and frame_path in camera_links_hls for one camera.
    Reclassification clears any prior manual verification: a fresh VLM result
    invalidates the operator's prior override, so the camera goes back to the
    untouched/VLM-only state until a human re-verifies it.
    """
    frame_path = _canonicalize_frame_path(frame_path)
    # Only overwrite frame_path when a fresh one is supplied — a frame_path-less
    # update (e.g. the checkpoint-resume path) must not wipe an existing capture.
    frame_set = "frame_path = %s," if frame_path is not None else ""
    sql = f"""
        UPDATE camera_links_hls
        SET classification = %s, status = %s, {frame_set}
            manually_verified = 0, verified_by = NULL
        WHERE exam_id = %s
          AND ch_code = %s
          AND is_active = 1
    """
    params = [classification, status]
    if frame_path is not None:
        params.append(frame_path)
    params += [exam_id, ch_code]
    if shift_id is not None:
        sql += " AND (shift_id = %s OR shift_id IS NULL)"
        params.append(shift_id)
    if exam_date is not None:
        sql += " AND (exam_date = %s OR exam_date IS NULL)"
        params.append(exam_date)
        
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(f"[DB] Error updating camera_links_hls classification (ch_code={ch_code}): {e}")
        return 0

def update_hls_status(exam_id: int, shift_id: int, exam_date: str, ch_code: str, status: str) -> int:
    """Update only status in camera_links_hls for one camera."""
    sql = """
        UPDATE camera_links_hls
        SET status = %s
        WHERE exam_id = %s
          AND ch_code = %s
          AND is_active = 1
    """
    params = [status, exam_id, ch_code]
    if shift_id is not None:
        sql += " AND (shift_id = %s OR shift_id IS NULL)"
        params.append(shift_id)
    if exam_date is not None:
        sql += " AND (exam_date = %s OR exam_date IS NULL)"
        params.append(exam_date)
        
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(f"[DB] Error updating camera_links_hls status (ch_code={ch_code}): {e}")
        return 0


# ============================================================
# 📡  RTSP CAMERAS
# ============================================================
def get_rtsp_cameras(exam_id: int) -> list:
    """
    Query camera_links_rtsp for cameras matching exam_id.
    Automatically filters cameras based on worker assignment for horizontal scaling.
    
    NOTE: This loads ALL cameras regardless of status - used by vision classification scripts.
    For detection workers, use get_rtsp_cameras_accessible() instead.

    Returns list of dicts with keys:
        cctv_url, device_id, classification, camera_name,
        centre_name, city, state, tc_code
    """
    # Uses idx_exam_active_rtsp (exam_id, is_active) - perfect composite index
    sql = """
        SELECT
            id, device_id, exam_id, centre_name, tc_code,
            camera_name, camera_no, city, state, cctv_url,
            is_active, classification, status
        FROM camera_links_rtsp USE INDEX (idx_exam_active_rtsp)
        WHERE exam_id  = %s
          AND is_active = 1
        ORDER BY id
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (exam_id,))
                rows = cur.fetchall()
                
                # Apply camera distribution filter for horizontal scaling
                try:
                    try:
                        from app.camera_distributor import filter_cameras
                    except ImportError:
                        import sys
                        from pathlib import Path
                        root_dir = str(Path(__file__).resolve().parents[3])
                        if root_dir not in sys.path:
                            sys.path.append(root_dir)
                        from app.camera_distributor import filter_cameras
                    rows = filter_cameras(rows)
                except Exception as e:
                    logger.warning(f"Failed to apply camera distribution filter: {e}")

                logger.info(
                    f"[DB] Loaded {len(rows)} RTSP cameras "
                    f"(exam_id={exam_id})"
                )
                return rows
    except Exception as e:
        logger.error(f"[DB] Error querying camera_links_rtsp: {e}")
        return []


def get_rtsp_cameras_accessible(exam_id: int) -> list:
    """
    Query camera_links_rtsp for ACCESSIBLE cameras only (status = 'Accessible').
    Used by detection workers - no point analyzing unreachable streams.
    Automatically filters cameras based on worker assignment for horizontal scaling.

    Returns list of dicts with keys:
        cctv_url, device_id, classification, camera_name,
        centre_name, city, state, tc_code
    """
    # Uses idx_exam_active_rtsp (exam_id, is_active) - perfect composite index
    sql = """
        SELECT
            id, device_id, exam_id, centre_name, tc_code,
            camera_name, camera_no, city, state, cctv_url,
            is_active, classification, status
        FROM camera_links_rtsp USE INDEX (idx_exam_active_rtsp)
        WHERE exam_id  = %s
          AND is_active = 1
          AND status = 'Accessible'
        ORDER BY id
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (exam_id,))
                rows = cur.fetchall()
                
                # Apply camera distribution filter for horizontal scaling
                try:
                    try:
                        from app.camera_distributor import filter_cameras
                    except ImportError:
                        import sys
                        from pathlib import Path
                        root_dir = str(Path(__file__).resolve().parents[3])
                        if root_dir not in sys.path:
                            sys.path.append(root_dir)
                        from app.camera_distributor import filter_cameras
                    rows = filter_cameras(rows)
                except Exception as e:
                    logger.warning(f"Failed to apply camera distribution filter: {e}")

                logger.info(
                    f"[DB] Loaded {len(rows)} ACCESSIBLE RTSP cameras "
                    f"(exam_id={exam_id})"
                )
                return rows
    except Exception as e:
        logger.error(f"[DB] Error querying camera_links_rtsp (accessible): {e}")
        return []

def get_rtsp_camera_by_id(camera_id_pk: int) -> dict:
    """Fetch one active RTSP camera row by its primary key ID."""
    if not camera_id_pk:
        return {}

    sql = """
        SELECT
            id, device_id, exam_id, centre_name, tc_code,
            camera_name, camera_no, city, state, cctv_url,
            is_active, classification
        FROM camera_links_rtsp
        WHERE id = %s
          AND is_active = 1
        LIMIT 1
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (camera_id_pk,))
                row = cur.fetchone()
                return row or {}
    except Exception as e:
        logger.error(f"[DB] Error fetching camera_links_rtsp by ID ({camera_id_pk}): {e}")
        return {}

def update_rtsp_classification(exam_id: int, device_id: str, camera_no: str, classification: str, status: str = None, frame_path: str = None) -> int:
    """Update classification, status, and frame_path in camera_links_rtsp for one camera.
    Reclassification clears any prior manual verification (see update_hls_classification).
    """
    frame_path = _canonicalize_frame_path(frame_path)
    sql = """
        UPDATE camera_links_rtsp
        SET classification = %s, status = %s, frame_path = %s,
            manually_verified = 0, verified_by = NULL
        WHERE exam_id = %s
          AND device_id = %s
          AND camera_no = %s
          AND is_active = 1
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (classification, status, frame_path, exam_id, device_id, camera_no))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(f"[DB] Error updating camera_links_rtsp classification (device_id={device_id}, camera_no={camera_no}): {e}")
        return 0

def update_rtsp_status(exam_id: int, device_id: str, camera_no: str, status: str) -> int:
    """Update only status in camera_links_rtsp for one camera."""
    sql = """
        UPDATE camera_links_rtsp
        SET status = %s
        WHERE exam_id = %s
          AND device_id = %s
          AND camera_no = %s
          AND is_active = 1
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (status, exam_id, device_id, camera_no))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(f"[DB] Error updating camera_links_rtsp status (device_id={device_id}, camera_no={camera_no}): {e}")
        return 0


def update_rtsp_status_and_frame_path(
    exam_id: int, device_id: str, camera_no: str,
    status: str, frame_path: Optional[str],
) -> int:
    """Update status and frame_path in camera_links_rtsp without touching classification."""
    frame_path = _canonicalize_frame_path(frame_path)
    sql = """
        UPDATE camera_links_rtsp
        SET status = %s, frame_path = %s
        WHERE exam_id = %s
          AND device_id = %s
          AND camera_no = %s
          AND is_active = 1
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (status, frame_path, exam_id, device_id, camera_no))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(
            f"[DB] Error updating rtsp status+frame_path (device_id={device_id}, camera_no={camera_no}): {e}"
        )
        return 0


# ============================================================
# 📚  CLASSIFICATION HISTORY  (append-only per-run log)
# ============================================================

def insert_hls_classification_history(
    camera_id: int,
    shift_id: Optional[int],
    exam_date: Optional[str],
    frame_path: Optional[str],
    classification: Optional[str],
) -> int:
    """Append one row to camera_classification_hls for this run."""
    if not camera_id:
        return 0
    frame_path = _canonicalize_frame_path(frame_path)
    sql = """
        INSERT INTO camera_classification_hls
            (camera_id, shift_id, exam_date, frame_path, classification)
        VALUES (%s, %s, %s, %s, %s)
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (camera_id, shift_id, exam_date, frame_path, classification))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(
            f"[DB] Error inserting camera_classification_hls (camera_id={camera_id}): {e}"
        )
        return 0


def insert_rtsp_classification_history(
    camera_id: int,
    shift_id: Optional[int],
    exam_date: Optional[str],
    frame_path: Optional[str],
    classification: Optional[str],
) -> int:
    """Append one row to camera_classification_rtsp for this run."""
    if not camera_id:
        return 0
    frame_path = _canonicalize_frame_path(frame_path)
    sql = """
        INSERT INTO camera_classification_rtsp
            (camera_id, shift_id, exam_date, frame_path, classification)
        VALUES (%s, %s, %s, %s, %s)
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (camera_id, shift_id, exam_date, frame_path, classification))
                conn.commit()
                return cur.rowcount
    except Exception as e:
        logger.error(
            f"[DB] Error inserting camera_classification_rtsp (camera_id={camera_id}): {e}"
        )
        return 0


def get_hls_camera_by_ch_code(ch_code: str, exam_date: str = None) -> dict:
    """Fetch one active HLS camera row by ch_code, optionally preferring a specific exam_date."""
    if not ch_code:
        return {}

    sql_with_date = """
        SELECT
            id, exam_id, shift_id, centre_name, city, state,
            camera_name, ch_code, tc_code, cctv_url,
            camera_category, classification, is_active, exam_date
        FROM camera_links_hls
        WHERE ch_code = %s
          AND is_active = 1
        ORDER BY
          CASE WHEN exam_date = %s THEN 0 ELSE 1 END,
          exam_date DESC,
          id DESC
        LIMIT 1
    """

    sql_without_date = """
        SELECT
            id, exam_id, shift_id, centre_name, city, state,
            camera_name, ch_code, tc_code, cctv_url,
            camera_category, classification, is_active, exam_date
        FROM camera_links_hls
        WHERE ch_code = %s
          AND is_active = 1
        ORDER BY exam_date DESC, id DESC
        LIMIT 1
    """

    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                if exam_date:
                    cur.execute(sql_with_date, (ch_code, exam_date))
                else:
                    cur.execute(sql_without_date, (ch_code,))
                row = cur.fetchone()
                if not row:
                    return {}
                return row
    except Exception as e:
        logger.error(f"[DB] Error querying camera_links_hls by ch_code ({ch_code}): {e}")
        return {}


def get_rtsp_camera_by_device_and_ch(device_id: str, camera_no: str) -> dict:
    """Fetch one active RTSP camera row by device_id and camera_no."""
    if not device_id or not camera_no:
        return {}

    sql = """
        SELECT
            id, device_id, exam_id, centre_name, tc_code,
            camera_name, camera_no, city, state, cctv_url,
            is_active, classification
        FROM camera_links_rtsp
        WHERE device_id = %s
          AND camera_no = %s
          AND is_active = 1
        LIMIT 1
    """
    try:
        conn = _get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (device_id, camera_no))
                row = cur.fetchone()
                return row or {}
    except Exception as e:
        logger.error(f"[DB] Error fetching camera_links_rtsp by device/ch ({device_id}/{camera_no}): {e}")
        return {}



# ============================================================
# 📝  HLS DETECTION REPORTING  (used by all detection scripts)
# ============================================================

# Base URL for detection images served by the backend
_IMAGE_BASE_URL = os.environ.get(
    "DETECTION_IMAGE_BASE_URL",
    "http://www.examscctvmonitoring.com/cctv_dev/serve-detection-images"
)

# Base URL for RTSP detection images
_RTSP_IMAGE_BASE_URL = os.environ.get(
    "DETECTION_RTSP_IMAGE_BASE_URL",
    "http://examscctvmonitoring.com/cctv_dev/serve-rtsp-detection-images"
)

# Path to the shared DB report log (script-server/scripts/logs/db_reports.log)
_LOG_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../logs/db_reports.log"))
os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)


def _extract_unique_id_from_image_name(image_name: str) -> Optional[str]:
    """Extract the detection UID from a saved image filename."""
    base_name = os.path.basename(str(image_name))
    full_uuid = re.search(
        r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\.[^.]+$|$)',
        base_name,
        re.IGNORECASE,
    )
    if full_uuid:
        return full_uuid.group(1).lower()

    short_uid = os.path.splitext(base_name)[0].split("_")[-1]
    if re.fullmatch(r'[0-9a-f]{8}', short_uid, re.IGNORECASE):
        return short_uid.lower()

    return None


def report_to_db_hls(ch_code: str, image_name: str, folder: str = "crowd_detection") -> bool:
    """
    Central HLS detection reporter.

    Looks up the camera row in camera_links_hls by ch_code + EXAM_ID/SHIFT_ID
    (read from environment), then inserts one row into camera_report_hls.

    Args:
        ch_code   : camera channel code (numeric string, e.g. "85721")
        image_name: filename of the saved detection image (no path)
        folder    : detection sub-folder name used in the image URL and
                    stored as detection_type (e.g. "crowd_detection_threshold")

    Returns:
        True on success, False on any failure.
    """
    unique_id = _extract_unique_id_from_image_name(image_name)
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(
                f"\n--- [{folder}] Report Attempt: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                f"Image: {image_name} | ch_code: {ch_code}\n"
            )

        # If ch_code is not numeric, try to extract it from the filename
        if not str(ch_code).isdigit():
            m = re.search(r'_ch(\d+)_', image_name)
            if m:
                ch_code = m.group(1)
                with open(_LOG_FILE, "a") as f:
                    f.write(f"Extracted ch_code from filename: {ch_code}\n")

        exam_id  = os.environ.get("EXAM_ID")
        shift_id = os.environ.get("SHIFT_ID")

        if not exam_id:
            msg = "[DB-REPORT] ❌ ERROR: EXAM_ID not set in environment.\n"
            logger.error(msg.strip())
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
            return False

        # Parse timestamp from filename (format: YYYYMMDD_HHMMSS)
        ts_m = re.search(r'(\d{8}_\d{6})', image_name)
        if ts_m:
            date_time = datetime.strptime(ts_m.group(1), "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M:%S")
        else:
            date_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                # Look up camera row
                cur.execute(
                    """
                    SELECT * FROM camera_links_hls
                    WHERE ch_code = %s AND exam_id = %s
                      AND (shift_id = %s OR shift_id IS NULL)
                    LIMIT 1
                    """,
                    (ch_code, exam_id, shift_id),
                )
                cam = cur.fetchone()

            if not cam:
                msg = (
                    f"[DB-REPORT] ❌ Camera NOT found for "
                    f"ch_code={ch_code}, exam_id={exam_id}\n"
                )
                logger.error(msg.strip())
                with open(_LOG_FILE, "a") as f:
                    f.write(msg)
                return False

            # Build derived fields
            camera_name      = cam.get('camera_name', '')
            parts            = camera_name.split('/')
            report_camera_id = f"{parts[0]}_{parts[2]}" if len(parts) >= 3 else camera_name
            status_raw       = str(cam.get('status', '')).lower()
            camera_status    = "Online" if status_raw == 'accessible' else "Offline"
            image_url        = f"{_IMAGE_BASE_URL}/{folder}/{image_name}"

            # Embed gpu_id in image EXIF before committing to DB
            hls_image_path = os.path.join(_DETECTION_BASE, "results", "tcs_detections", folder, image_name)
            _embed_gpu_id_in_image(hls_image_path, _GPU_ID)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO camera_report_hls (
                        exam_id, shift_id, city, center_name, camera_id,
                        camera_name, camera_category, tc_code, ch_code,
                        state, camera_status, events, date_time,
                        imageurl, detection_type, unique_id, gpu_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cam.get('exam_id'), cam.get('shift_id'), cam.get('city'),
                        cam.get('centre_name'), report_camera_id, camera_name,
                        cam.get('camera_category'), cam.get('tc_code'), ch_code,
                        cam.get('state'), camera_status, 1, date_time, image_url, folder,
                        unique_id, _GPU_ID,
                    ),
                )
            conn.commit()
            msg = f"[DB-REPORT] ✅ SUCCESS: {folder} report inserted for ch_code={ch_code} unique_id={unique_id} gpu_id={_GPU_ID}\n"
            logger.info(msg.strip())
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
            return True

        finally:
            conn.close()

    except Exception as e:
        msg = f"[DB-REPORT] ❌ FATAL ERROR ({folder}): {e}\n"
        logger.error(msg.strip())
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
        except Exception:
            pass
        return False


# ============================================================
# 📝  RTSP DETECTION REPORTING
# ============================================================

def report_to_db_rtsp(device_id: str, camera_no: str, image_name: str, folder: str = "crowd_detection") -> bool:
    """
    Central RTSP detection reporter.

    Looks up the camera row in camera_links_rtsp by device_id + camera_no,
    and inserts one row into camera_report_rtsp.

    Args:
        device_id : RTSP device ID
        camera_no : RTSP camera number
        image_name: filename of the saved detection image (no path)
        folder    : detection sub-folder name used in the image URL and
                    stored as detection_type.

    Returns:
        True on success, False on any failure.
    """
    unique_id = _extract_unique_id_from_image_name(image_name)

    # Handle potentially missing device/camera info if they weren't explicitly passed
    if not device_id or not camera_no:
        parts = image_name.split('_')
        if len(parts) >= 4 and not device_id:
            device_id = parts[1]
        if len(parts) >= 4 and not camera_no:
            camera_no = parts[2]

    try:
        with open(_LOG_FILE, "a") as f:
            f.write(
                f"\n--- [{folder}] RTSP Report Attempt: "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                f"Image: {image_name} | device_id: {device_id} | camera_no: {camera_no}\n"
            )

        exam_id  = os.environ.get("EXAM_ID")
        shift_id = os.environ.get("SHIFT_ID")

        if not exam_id:
            msg = "[DB-REPORT-RTSP] ❌ ERROR: EXAM_ID not set in environment.\n"
            logger.error(msg.strip())
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
            return False

        # Parse timestamp from filename (format: YYYYMMDD_HHMMSS)
        ts_m = re.search(r'(\d{8}_\d{6})', image_name)
        if ts_m:
            date_time = datetime.strptime(ts_m.group(1), "%Y%m%d_%H%M%S").strftime("%d/%m/%Y %H:%M:%S")
        else:
            date_time = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

        conn = _get_connection()
        try:
            with conn.cursor() as cur:
                # Look up camera row
                cur.execute(
                    """
                    SELECT * FROM camera_links_rtsp
                    WHERE device_id = %s AND camera_no = %s AND exam_id = %s
                    LIMIT 1
                    """,
                    (device_id, camera_no, exam_id),
                )
                cam = cur.fetchone()

            if not cam:
                msg = (
                    f"[DB-REPORT-RTSP] ❌ Camera NOT found for "
                    f"device_id={device_id}, camera_no={camera_no}, exam_id={exam_id}\n"
                )
                logger.error(msg.strip())
                with open(_LOG_FILE, "a") as f:
                    f.write(msg)
                return False

            camera_name      = cam.get('camera_name', '')
            status_raw       = str(cam.get('status', '')).lower()
            camera_status    = "Online" if status_raw == 'accessible' else "Offline"
            image_url        = f"{_RTSP_IMAGE_BASE_URL}/{folder}/{image_name}"

            # Embed gpu_id in image EXIF before committing to DB
            rtsp_image_path = os.path.join(_DETECTION_BASE, "results", folder, image_name)
            _embed_gpu_id_in_image(rtsp_image_path, _GPU_ID)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO camera_report_rtsp (
                        exam_id, shift_id, city, center_name, camera_id,
                        device_id, camera_no, camera_name, tc_code,
                        state, camera_status, events, date_time,
                        imageurl, detection_type, unique_id, gpu_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        cam.get('exam_id'), shift_id, cam.get('city'),
                        cam.get('centre_name'), f"{device_id}_{camera_no}", device_id, camera_no,
                        camera_name, cam.get('tc_code'), cam.get('state'),
                        camera_status, 1, date_time, image_url, folder,
                        unique_id, _GPU_ID,
                    ),
                )
            conn.commit()
            msg = f"[DB-REPORT-RTSP] ✅ SUCCESS: {folder} report inserted for device_id={device_id} unique_id={unique_id} gpu_id={_GPU_ID}\n"
            logger.info(msg.strip())
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
            return True

        finally:
            conn.close()

    except Exception as e:
        msg = f"[DB-REPORT-RTSP] ❌ FATAL ERROR ({folder}): {e}\n"
        logger.error(msg.strip())
        try:
            with open(_LOG_FILE, "a") as f:
                f.write(msg)
        except Exception:
            pass
        return False


# ============================================================
# 🧪  STANDALONE TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from datetime import date

    today = date.today().isoformat()
    print(f"\n=== Testing HLS cameras (exam_id=2, shift_id=7, date={today}) ===")
    hls = get_hls_cameras(2, 7, today)
    for cam in hls[:3]:
        print(f"  ch_code={cam['ch_code']}  url={str(cam['cctv_url'])[:60]}...")

    print(f"\n=== Testing RTSP cameras (exam_id=2) ===")
    rtsp = get_rtsp_cameras(2)
    for cam in rtsp[:3]:
        print(f"  device_id={cam['device_id']}  url={str(cam['cctv_url'])[:60]}...")
