-- Additive migration: session_log_raw table for the new collectors/session_logs.py
-- collector. Applied idempotently by install.sh on next install.
--
-- Per flightplan/docs/SRS.md §6.3.2 + §8.4.

CREATE TABLE IF NOT EXISTS session_log_raw (
  uid             TEXT PRIMARY KEY,        -- "sl-<host>-<sha256(rel_filepath)[:8]>-<mtime_ns>"
  scope           TEXT NOT NULL,           -- 'global' | 'project'
  source_project  TEXT,                    -- repo basename when scope='project'
  rel_filepath    TEXT NOT NULL,           -- relative to ~ or to repo root
  abs_filepath    TEXT NOT NULL,           -- absolute path on this device
  kind            TEXT NOT NULL,           -- 'session' | 'handoff' | 'mine-report'
  log_date        TEXT,                    -- parsed from filename YYYY-MM-DD
  log_time_local  TEXT,                    -- parsed from filename HH:MM
  topic           TEXT,
  raw_md          TEXT NOT NULL,
  raw_md_sha256   TEXT NOT NULL,
  mtime           TEXT NOT NULL,           -- ISO UTC of file mtime
  size_bytes      INTEGER,
  collected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_log_raw_date
  ON session_log_raw(log_date);
CREATE INDEX IF NOT EXISTS idx_session_log_raw_project
  ON session_log_raw(source_project);
CREATE INDEX IF NOT EXISTS idx_session_log_raw_collected
  ON session_log_raw(collected_at);
