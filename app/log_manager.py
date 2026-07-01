import os
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from app.config import LOGS_DIR
from app.logger import logger

IST = timezone(timedelta(hours=5, minutes=30))

class LogManager:
    def __init__(self):
        self.logs_dir = Path(LOGS_DIR)
        self.ensure_log_directory()

    def ensure_log_directory(self):
        if not self.logs_dir.exists():
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created logs directory: {self.logs_dir}")

    def get_log_file_path(self, execution_id, date=None):
        if date is None:
            date = datetime.now(IST)
        date_str = date.strftime("%Y-%m-%d")
        day_dir = self.logs_dir / date_str
        
        if not day_dir.exists():
            day_dir.mkdir(parents=True, exist_ok=True)
            
        return day_dir / f"execution-{execution_id}.log"

    def write_log(self, execution_id, message, level="INFO"):
        try:
            log_path = self.get_log_file_path(execution_id)
            timestamp = datetime.now(IST).isoformat()
            log_entry = f"[{timestamp}] [{level}] {message}\n"
            
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except Exception as e:
            logger.error(f"Failed to write log for execution {execution_id}: {e}")

    def read_logs(self, execution_id, date=None):
        try:
            log_path = self.get_log_file_path(execution_id, date)
            if not log_path.exists():
                return None
            with open(log_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Failed to read logs for execution {execution_id}: {e}")
            return None

    def get_log_files_for_date(self, date=None):
        if date is None:
            date = datetime.now(IST)
        date_str = date.strftime("%Y-%m-%d")
        day_dir = self.logs_dir / date_str
        
        if not day_dir.exists():
            return []
            
        files = []
        for file_path in day_dir.iterdir():
            if file_path.is_file() and file_path.name.endswith(".log"):
                import re
                match = re.search(r"execution-([\w-]+)\.log", file_path.name)
                execution_id = match.group(1) if match else None
                files.append({
                    "filename": file_path.name,
                    "path": str(file_path),
                    "executionId": execution_id
                })
        return files

    def cleanup_old_logs(self, days_to_keep=30):
        try:
            import time
            cutoff = time.time() - (days_to_keep * 24 * 60 * 60)
            
            for item in self.logs_dir.iterdir():
                if item.is_dir():
                    try:
                        dir_date = datetime.strptime(item.name, "%Y-%m-%d")
                        if dir_date.timestamp() < cutoff:
                            shutil.rmtree(item)
                            logger.info(f"Cleaned up old logs: {item.name}")
                    except ValueError:
                        pass
        except Exception as e:
            logger.error(f"Failed to cleanup old logs: {e}")

    def get_log_summary(self, execution_id, date=None):
        logs = self.read_logs(execution_id, date)
        if not logs:
            return None
            
        lines = [line.strip() for line in logs.split("\n") if line.strip()]
        error_lines = [line for line in lines if "[ERROR]" in line]
        warn_lines = [line for line in lines if "[WARN]" in line]
        
        return {
            "totalLines": len(lines),
            "errors": len(error_lines),
            "warnings": len(warn_lines),
            "lastLine": lines[-1] if lines else "",
            "hasErrors": len(error_lines) > 0
        }

log_manager = LogManager()
