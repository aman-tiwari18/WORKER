-- Camera uptime checker tables. Idempotent: safe to run again.
-- Times are DATETIME(3) in IST (the service sets time_zone '+05:30' on every connection).
-- No foreign keys on purpose: deleting an exam must not wipe its uptime history.

CREATE TABLE IF NOT EXISTS camera_uptime_runs (
  id               INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  exam_id          INT NOT NULL,
  shift_id         INT NOT NULL,
  exam_date        DATE NOT NULL,
  exam_name        VARCHAR(255) NULL,
  link_type        VARCHAR(8) NOT NULL,
  shift_name       VARCHAR(100) NULL,
  shift_start      TIME NULL,
  shift_end        TIME NULL,
  window_start     DATETIME NOT NULL,
  window_end       DATETIME NOT NULL,
  status           ENUM('RUNNING','COMPLETED','STOPPED','INTERRUPTED') NOT NULL DEFAULT 'RUNNING',
  cameras_total    INT NOT NULL DEFAULT 0,
  cameras_checked  INT NOT NULL DEFAULT 0,
  cameras_up_once  INT NOT NULL DEFAULT 0,
  checks_total     INT NOT NULL DEFAULT 0,
  passes_completed INT NOT NULL DEFAULT 0,
  paused_seconds   INT NOT NULL DEFAULT 0,
  config_json      JSON NULL,
  started_at       DATETIME(3) NULL,
  ended_at         DATETIME(3) NULL,
  updated_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_uptime_run (exam_id, shift_id, exam_date),
  KEY idx_uptime_run_status (status),
  KEY idx_uptime_run_date (exam_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS camera_uptime_checks (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  check_uuid     CHAR(36) CHARACTER SET ascii NOT NULL,
  run_id         INT UNSIGNED NOT NULL,
  exam_id        INT NOT NULL,
  shift_id       INT NOT NULL,
  exam_date      DATE NOT NULL,
  link_type      VARCHAR(8) NOT NULL,
  camera_row_id  INT NULL,
  camera_key     VARCHAR(255) CHARACTER SET ascii NOT NULL,
  dvr_key        VARCHAR(255) CHARACTER SET ascii NULL,
  server         VARCHAR(128) NULL,
  tc_code        VARCHAR(255) NULL,
  checked_at     DATETIME(3) NOT NULL,
  result         ENUM('UP','DOWN','UNKNOWN') NOT NULL,
  reason         VARCHAR(64) NULL,
  stage          VARCHAR(16) NULL,
  http_status    SMALLINT NULL,
  api_code       INT NULL,
  frame_blank    TINYINT(1) NULL,
  startvideo_ms  INT NULL,
  ffmpeg_ms      INT NULL,
  total_ms       INT NULL,
  UNIQUE KEY uq_uptime_check_uuid (check_uuid),
  KEY idx_uptime_check_run_cam (run_id, camera_key, checked_at),
  KEY idx_uptime_check_time (checked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS camera_uptime_summary (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  run_id           INT UNSIGNED NOT NULL,
  exam_id          INT NOT NULL,
  shift_id         INT NOT NULL,
  exam_date        DATE NOT NULL,
  link_type        VARCHAR(8) NOT NULL,
  camera_key       VARCHAR(255) CHARACTER SET ascii NOT NULL,
  camera_row_id    INT NULL,
  camera_name      VARCHAR(255) NULL,
  centre_name      VARCHAR(255) NULL,
  tc_code          VARCHAR(255) NULL,
  city             VARCHAR(255) NULL,
  state            VARCHAR(255) NULL,
  server           VARCHAR(128) NULL,
  dvr_key          VARCHAR(255) CHARACTER SET ascii NULL,
  in_roster        TINYINT(1) NOT NULL DEFAULT 1,
  checks_total     INT NOT NULL DEFAULT 0,
  checks_up        INT NOT NULL DEFAULT 0,
  checks_down      INT NOT NULL DEFAULT 0,
  checks_unknown   INT NOT NULL DEFAULT 0,
  checks_blank     INT NOT NULL DEFAULT 0,
  first_checked_at DATETIME(3) NULL,
  last_checked_at  DATETIME(3) NULL,
  first_up_at      DATETIME(3) NULL,
  last_up_at       DATETIME(3) NULL,
  last_down_at     DATETIME(3) NULL,
  last_result      ENUM('NOT_CHECKED','UP','DOWN','UNKNOWN') NOT NULL DEFAULT 'NOT_CHECKED',
  last_reason      VARCHAR(64) NULL,
  last_frame_path  VARCHAR(255) NULL,
  last_frame_at    DATETIME(3) NULL,
  down_since       DATETIME(3) NULL,           -- DOWN check that opened the current down period
  down_reason      VARCHAR(64) NULL,           -- its reason (no_frame, no_stream, ...)
  outages          INT NOT NULL DEFAULT 0,     -- down periods started in this run
  downtime_s       INT NOT NULL DEFAULT 0,     -- seconds in closed down periods
  downtime_json    JSON NULL,                  -- [{down_at, up_at, down_s, reason}, ...], see uptime/downtime.py
  updated_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_uptime_summary_run_cam (run_id, camera_key),
  KEY idx_uptime_summary_exam (exam_id, exam_date, shift_id),
  KEY idx_uptime_summary_run_tc (run_id, tc_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS camera_uptime_events (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  at          DATETIME(3) NOT NULL,
  scope       VARCHAR(16) NOT NULL,
  scope_key   VARCHAR(255) NULL,
  event       VARCHAR(16) NOT NULL,
  reason      VARCHAR(64) NULL,
  detail_json JSON NULL,
  run_id      INT UNSIGNED NULL,
  KEY idx_uptime_event_time (at),
  KEY idx_uptime_event_scope (scope, scope_key, at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per running checker (one per server). Auto sharding (UPTIME_SHARD_COUNT=0) sorts the instances seen
-- in the last UPTIME_INSTANCE_TTL_S seconds and gives each its share of the DVRs; a clean stop deletes the row.
CREATE TABLE IF NOT EXISTS camera_uptime_instances (
  worker_id   VARCHAR(128) NOT NULL PRIMARY KEY,
  host        VARCHAR(255) NULL,
  started_at  DATETIME(3) NOT NULL,
  last_seen   DATETIME(3) NOT NULL,
  KEY idx_uptime_instance_seen (last_seen)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3 Nov 2026: down periods on an existing camera_uptime_summary (CREATE above already has them):
-- ALTER TABLE camera_uptime_summary
--   ADD COLUMN down_since DATETIME(3) NULL AFTER last_frame_at,
--   ADD COLUMN down_reason VARCHAR(64) NULL AFTER down_since,
--   ADD COLUMN outages INT NOT NULL DEFAULT 0 AFTER down_reason,
--   ADD COLUMN downtime_s INT NOT NULL DEFAULT 0 AFTER outages,
--   ADD COLUMN downtime_json JSON NULL AFTER downtime_s;
