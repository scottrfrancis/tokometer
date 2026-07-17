-- Copilot-in-VS-Code observability: failure/diagnostic events + manual observations.
-- Companion to the usage table (schema.sql); populated by collectors/copilot_chat_log.py,
-- collectors/vscode_events.py, and collectors/copilot_observe.py. Local only.

-- one row per observed event (power throttle, quota header, crash string, downgrade label…)
CREATE TABLE IF NOT EXISTS event (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  uid        TEXT NOT NULL UNIQUE,   -- deterministic dedup key (file:line based)
  ts         TEXT NOT NULL,          -- ISO-8601 UTC of the event
  harness    TEXT NOT NULL DEFAULT 'copilot',
  kind       TEXT NOT NULL,          -- power_throttle|quota|context_budget|tool_result_disk|
                                     -- worker_oom|v8_oom|exthost_crash|exthost_restart|
                                     -- listener_leak|request_failure|model_downgrade
  mechanism  TEXT,                   -- classifier label: downroute|context-rot|quota|
                                     -- client-oom|client-oom-reroute|power (NULL = n/a)
  detail     TEXT,                   -- JSON payload of extracted fields
  session_id TEXT,                   -- conversation/session id when known
  source     TEXT NOT NULL,          -- vscode-chat-log|vscode-session-log|classifier
  raw_ref    TEXT                    -- path:line the event came from (audit)
);
CREATE INDEX IF NOT EXISTS idx_event_ts   ON event(ts);
CREATE INDEX IF NOT EXISTS idx_event_kind ON event(kind);

-- one row per human observation: the quality rating no log can record, and the
-- benign-continue vs silent-stall distinction.
CREATE TABLE IF NOT EXISTS manual_obs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      TEXT NOT NULL,             -- ISO-8601 UTC
  kind    TEXT NOT NULL DEFAULT 'rating',  -- rating|continue_prompt|stall
  quality INTEGER,                   -- 1-5 (NULL for continue_prompt/stall marks)
  model   TEXT,                      -- optional: model hover/fingerprint if noted
  note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_manual_obs_ts ON manual_obs(ts);
