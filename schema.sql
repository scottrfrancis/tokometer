-- ~/.tokometer/ledger.db  (WAL; never leaves this machine)
PRAGMA journal_mode = WAL;

-- one normalized row per harvested usage record
CREATE TABLE IF NOT EXISTS usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  uid           TEXT NOT NULL UNIQUE,     -- deterministic dedup key set by the collector
  ts            TEXT NOT NULL,            -- ISO-8601 UTC of the activity (not harvest time)
  harness       TEXT NOT NULL,            -- opencode|claude-code|droid|copilot|cursor|local
  provider      TEXT,                     -- factory|cursor|github|anthropic|openai|ollama|mlx|...
  model         TEXT,
  session_id    TEXT,
  request_id    TEXT,                     -- cursor join key when available
  input_tokens       INTEGER DEFAULT 0,
  output_tokens      INTEGER DEFAULT 0,
  cache_read_tokens  INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  reasoning_tokens   INTEGER DEFAULT 0,
  credits            INTEGER DEFAULT 0,   -- provider-native metered unit (e.g. factoryCredits)
  total_tokens  INTEGER GENERATED ALWAYS AS
                (input_tokens+output_tokens+cache_read_tokens+cache_write_tokens+reasoning_tokens) STORED,
  cost_usd      REAL DEFAULT 0.0,
  source        TEXT NOT NULL,            -- session-file|cli-json|admin-api|scrape|self-tally
  confidence    TEXT NOT NULL DEFAULT 'exact', -- exact|estimate
  raw_ref       TEXT,                     -- path/line/url the record came from (for audit)
  cwd           TEXT,                     -- working directory for repo attribution (NULL = unknown)
  account       TEXT,                     -- authenticated identity (e.g. email/login) the harness used; NULL = unknown
  subscription  TEXT,                     -- plan tier consumed: max|pro|team|enterprise|api|free (provider-native; NULL = unknown)
  org           TEXT                      -- friendly org/account grouping label for reports (e.g. Personal|Employer|Client); NULL = unknown
);
CREATE INDEX IF NOT EXISTS idx_usage_ts       ON usage(ts);
CREATE INDEX IF NOT EXISTS idx_usage_harness  ON usage(harness);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider);
CREATE INDEX IF NOT EXISTS idx_usage_account  ON usage(account);
CREATE INDEX IF NOT EXISTS idx_usage_org      ON usage(org);

-- per-commit code metrics (git), classified by file role
CREATE TABLE IF NOT EXISTS commit_metric (
  repo       TEXT NOT NULL,               -- repo dir name under the scan root
  repo_path  TEXT NOT NULL,               -- absolute path (attribution join key)
  sha        TEXT NOT NULL,
  ts         TEXT NOT NULL,               -- author date, ISO-8601 UTC
  author     TEXT,
  files      INTEGER DEFAULT 0,
  code_add   INTEGER DEFAULT 0, code_del  INTEGER DEFAULT 0,
  docs_add   INTEGER DEFAULT 0, docs_del  INTEGER DEFAULT 0,
  test_add   INTEGER DEFAULT 0, test_del  INTEGER DEFAULT 0,
  is_merge   INTEGER DEFAULT 0,
  PRIMARY KEY (repo_path, sha)
);
CREATE INDEX IF NOT EXISTS idx_commit_ts   ON commit_metric(ts);
CREATE INDEX IF NOT EXISTS idx_commit_repo ON commit_metric(repo_path);

-- merged/published PRs (GitHub via gh)
CREATE TABLE IF NOT EXISTS pr_metric (
  repo        TEXT NOT NULL,              -- owner/name
  number      INTEGER NOT NULL,
  title       TEXT,
  state       TEXT,                       -- merged|open|closed
  created_at  TEXT,
  merged_at   TEXT,
  additions   INTEGER DEFAULT 0,
  deletions   INTEGER DEFAULT 0,
  PRIMARY KEY (repo, number)
);
CREATE INDEX IF NOT EXISTS idx_pr_merged ON pr_metric(merged_at);

-- Cursor repo attribution: per-hour repo activity from Cursor's local AI-code
-- tracking DB (the usage CSV is repo-blind). Used to infer which repo Cursor's
-- output tokens belong to, by matching the hour. Heuristic, marked accordingly.
CREATE TABLE IF NOT EXISTS cursor_repo_hour (
  hour TEXT NOT NULL,        -- 'YYYY-MM-DDTHH' local-time bucket
  repo TEXT NOT NULL,
  hits INTEGER NOT NULL,     -- AI-code records in that hour for that repo
  PRIMARY KEY (hour, repo)
);
CREATE INDEX IF NOT EXISTS idx_cursor_repo_hour ON cursor_repo_hour(hour);
