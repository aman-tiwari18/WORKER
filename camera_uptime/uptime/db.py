"""MySQL access: one connection per thread, IST session time zone, autocommit."""
from __future__ import annotations

import logging
import threading

import pymysql
from pymysql.cursors import DictCursor

log = logging.getLogger("uptime.db")

OWN_TABLES = (
    "camera_uptime_runs",
    "camera_uptime_checks",
    "camera_uptime_summary",
    "camera_uptime_events",
)
ER_NO_SUCH_TABLE = 1146


class Database:
    def __init__(self, settings):
        self._s = settings
        self._local = threading.local()

    def _connect(self):
        s = self._s
        return pymysql.connect(
            host=s.db_host,
            port=s.db_port,
            user=s.db_user,
            password=s.db_password,
            database=s.db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
            # Naive DATETIMEs in the uptime tables are IST, like the rest of the schedule data.
            init_command="SET time_zone = '+05:30'",
        )

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.ping(reconnect=True)
                return c
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
        c = self._connect()
        self._local.conn = c
        return c

    # Empty params are passed as None: PyMySQL %-formats the SQL whenever args is not None.
    def query(self, sql: str, params=()) -> list[dict]:
        with self.conn().cursor() as cur:
            cur.execute(sql, params or None)
            return list(cur.fetchall())

    def execute(self, sql: str, params=()) -> int:
        with self.conn().cursor() as cur:
            return cur.execute(sql, params or None)

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    def missing_tables(self) -> list[str]:
        rows = self.query(
            "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN (%s, %s, %s, %s)",
            OWN_TABLES,
        )
        have = {r["t"] for r in rows}
        return [t for t in OWN_TABLES if t not in have]

    def has_table(self, name: str) -> bool:
        return bool(self.query("SELECT 1 AS x FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
                               "AND TABLE_NAME = %s", (name,)))
