"""
API Client for CCTV Pipeline
==============================
Fetches pending execution tasks from the CCTV backend API.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

# ================= API CONFIG =================
API_BASE_URL = os.environ.get(
    "CCTV_API_URL",
    os.environ.get("MAIN_BACKEND_URL", "http://localhost:3000/cctv_analytics/api")
)
API_TOKEN = os.environ.get("CCTV_API_TOKEN", "")


def get_pending_executions(api_url: str = None, token: str = None) -> list:
    """
    GET /cctv/api/executions/pending

    Returns list of pending execution dicts, each containing:
        script_code, exam_id, shift_id, link_type,
        script_name, exam_name, shift_name, ...
    """
    url = f"{api_url or API_BASE_URL}/executions/pending"
    auth_token = token or API_TOKEN

    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        data = resp.json()

        if not data.get("success"):
            logger.warning(f"[API] Response success=false: {data}")
            return []

        executions = data.get("data", [])

        # Extract execution_metadata into top-level keys for convenience
        result = []
        for ex in executions:
            meta = ex.get("execution_metadata", {})
            result.append({
                "id":               ex.get("id"),
                "task_name":        ex.get("task_name"),
                "duration_minutes": ex.get("duration_minutes"),
                "shift_name":       ex.get("shift_name"),
                "exam_name":        ex.get("exam_name"),
                "scheduled_at":     ex.get("scheduled_at"),
                "end_at":           ex.get("end_at"),
                # From metadata
                "assignment_id":    meta.get("assignment_id"),
                "script_id":        meta.get("script_id"),
                "script_code":      meta.get("script_code"),
                "script_name":      meta.get("script_name"),
                "shift_id":         meta.get("shift_id"),
                "exam_id":          meta.get("exam_id"),
                "link_type":        meta.get("link_type", "").upper(),
            })

        logger.info(f"[API] Fetched {len(result)} pending executions")
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"[API] Error fetching pending executions: {e}")
        return []


# ============================================================
# 🧪  STANDALONE TEST
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    execs = get_pending_executions()
    for ex in execs:
        print(
            f"  [{ex['script_code']}] exam_id={ex['exam_id']} "
            f"shift_id={ex['shift_id']} link_type={ex['link_type']}"
        )
