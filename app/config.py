import os
from dotenv import load_dotenv

load_dotenv()

def _clean_env_url(url: str) -> str:
    """Clean environment variable URLs from common typos like backticks or trailing braces."""
    if not url:
        return url
    return url.strip(" '\"`{}")

# Main Backend Connection
MAIN_BACKEND_URL = _clean_env_url(os.getenv("MAIN_BACKEND_URL", "http://localhost:3000/cctv_analytics/api"))
API_KEY = os.getenv("API_KEY", "cctv-monitoring-secret-key-2024")

# Database Configuration
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "rootpassword123")
DB_NAME = os.getenv("DB_NAME", "cctv_monitoring")

# Polling Configuration
POLL_INTERVAL_MS = int(os.getenv("POLL_INTERVAL_MS", 30000))
MAX_CONCURRENT_SCRIPTS = int(os.getenv("MAX_CONCURRENT_SCRIPTS", 5))
LOOKAHEAD_MINUTES = int(os.getenv("LOOKAHEAD_MINUTES", 3))
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", 30))

# Script Configuration
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", "./scripts")
_LOGS_DIR_RAW = os.getenv("LOGS_DIR", "./scripts/logs/executions")
LOGS_DIR = (
    os.path.join(SCRIPTS_DIR, "logs", "executions")
    if _LOGS_DIR_RAW.strip().replace("\\", "/") in ("./logs/executions", "logs/executions")
    else _LOGS_DIR_RAW
)

# HTTP Server Port
HTTP_PORT = int(os.getenv("HTTP_PORT", 8000))

# Worker Configuration (Horizontal Scaling)
WORKER_ID = os.getenv("WORKER_ID", "worker-0")
WORKER_NAME = os.getenv("WORKER_NAME", "Main Server (Worker 0)")
WORKER_IP = os.getenv("WORKER_IP", "")
DISTRIBUTION_MODE = os.getenv("DISTRIBUTION_MODE", "auto")
TOTAL_WORKERS = int(os.getenv("TOTAL_WORKERS", 4))
