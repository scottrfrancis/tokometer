"""Shared helpers for the tokometer ledger: connection, idempotent upsert, high-water state.

Local-only. stdlib only. Imported by every collector.
"""
import os
import json
import sqlite3
import datetime as dt

TOKOMETER_HOME = os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer"))
DB_PATH = os.path.join(TOKOMETER_HOME, "ledger.db")
STATE_DIR = os.path.join(TOKOMETER_HOME, "state")

USAGE_COLS = (
    "uid", "ts", "harness", "provider", "model", "session_id", "request_id",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "credits", "cost_usd", "source", "confidence", "raw_ref",
    "cwd", "account", "subscription", "org",
)


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode = WAL;")
    con.execute("PRAGMA busy_timeout = 30000;")
    return con


def insert_usage(con, rows, on_conflict="ignore"):
    """Insert usage rows keyed by USAGE_COLS.

    on_conflict='ignore'  -> append-only, dedupe by uid (immutable records).
    on_conflict='replace' -> upsert by uid, refreshing mutable totals (e.g. a
                             Droid session whose token counts grow during use).
    Returns the net change in row/field count.
    """
    cols = ",".join(USAGE_COLS)
    placeholders = ",".join("?" for _ in USAGE_COLS)
    if on_conflict == "replace":
        updates = ",".join(f"{c}=excluded.{c}" for c in USAGE_COLS if c != "uid")
        sql = (f"INSERT INTO usage ({cols}) VALUES ({placeholders}) "
               f"ON CONFLICT(uid) DO UPDATE SET {updates}")
    else:
        sql = f"INSERT OR IGNORE INTO usage ({cols}) VALUES ({placeholders})"
    before = con.total_changes
    con.executemany(sql, [tuple(r.get(c) for c in USAGE_COLS) for r in rows])
    con.commit()
    return con.total_changes - before


def load_state(harness):
    path = os.path.join(STATE_DIR, f"{harness}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(harness, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{harness}.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def iso_utc(epoch_ms=None, epoch_s=None):
    """Normalize a timestamp to ISO-8601 UTC (seconds precision, 'Z')."""
    if epoch_ms is not None:
        t = dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc)
    elif epoch_s is not None:
        t = dt.datetime.fromtimestamp(epoch_s, tz=dt.timezone.utc)
    else:
        t = dt.datetime.now(tz=dt.timezone.utc)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def retention_cutoff_date():
    """First day of the previous month (local). Collectors must NOT re-introduce
    rows older than this, matching monthly.py's prune horizon (keep current + prev
    month); otherwise re-scanning collectors would resurrect pruned history."""
    today = dt.date.today()
    first_this = today.replace(day=1)
    return (first_this - dt.timedelta(days=1)).replace(day=1)


def older_than_retention(iso_ts):
    """True if an ISO-8601 'Z' timestamp falls before the retention cutoff."""
    if not iso_ts:
        return False
    try:
        d = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone().date()
    except ValueError:
        return False
    return d < retention_cutoff_date()


def normalize_iso(s):
    """Coerce an arbitrary ISO string to UTC 'Z' form; passthrough on failure."""
    if not s:
        return iso_utc()
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return s
