#!/usr/bin/env python3
"""VS Code session-log crash scanner — the client-side failure companion.

Scans %APPDATA%/Code/logs/<session>/ (main.log, renderer*.log, exthost*.log —
every *.log except the Copilot Chat output channel, which copilot_chat_log.py
owns) for the crash-family strings that explain "Copilot hung / got dumber":

    Worker terminated due to reaching memory limit: JS heap out of memory
    OOM error in V8: Reached heap limit
    Extension host (… ) terminated unexpectedly. Code: 133
    Automatically restarting the extension host
    potential listener LEAK detected            (early warning)

Empty session dirs are normal (windowless launches — observed on the target box).
Local-only; stdlib-only; Python 3.11-compatible.
"""
import os
import re
import sys
import json
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "copilot"
SOURCE = "vscode-session-log"
STATE_KEY = "vscode_events"

_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
DEFAULT_ROOT = os.path.join(_APPDATA, "Code", "logs")

# pattern -> (event kind, mechanism label or None)
PATTERNS = [
    (re.compile(r"Worker terminated due to reaching memory limit: JS heap out of memory"),
     ("worker_oom", "client-oom")),
    (re.compile(r"OOM error in V8: Reached heap limit"),
     ("v8_oom", "client-oom")),
    (re.compile(r"terminated unexpectedly\. Code: 133"),
     ("exthost_crash", "client-oom")),
    (re.compile(r"Automatically restarting the extension host"),
     ("exthost_restart", "client-oom")),
    (re.compile(r"potential listener LEAK detected"),
     ("listener_leak", None)),
]
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) ")


def classify_line(line):
    """Return the event kind for a log line, or None."""
    for rx, (kind, _mech) in PATTERNS:
        if rx.search(line):
            return kind
    return None


def _mechanism_for(kind):
    for _rx, (k, mech) in PATTERNS:
        if k == kind:
            return mech
    return None


def _line_ts(line, fallback_mtime):
    m = _TS_RE.match(line)
    if m:
        t = dt.datetime.fromisoformat(m.group(1)).astimezone(dt.timezone.utc)
        return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return ledger.iso_utc(epoch_s=fallback_mtime)


def _iter_log_files(root):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".log"):
                continue
            if "GitHub Copilot Chat" in name:
                continue   # owned by copilot_chat_log.py
            yield os.path.join(dirpath, name)


def collect(con, logs_root=None, force=False, dry_run=False):
    root = logs_root or os.environ.get("VSCODE_LOGS_ROOT", DEFAULT_ROOT)
    result = {"files": 0, "events": 0, "inserted": 0}
    if not os.path.isdir(root):
        print(f"[{STATE_KEY}] no VS Code logs dir at {root}; skipping", file=sys.stderr)
        return result

    state = ledger.load_state(STATE_KEY)
    seen_mtime = {} if force else state.get("file_mtime", {})
    new_mtime = dict(state.get("file_mtime", {}))

    rows = []
    for path in sorted(_iter_log_files(root)):
        mtime = os.path.getmtime(path)
        if seen_mtime.get(path) == mtime:
            continue
        new_mtime[path] = mtime
        result["files"] += 1
        rel = os.path.relpath(path, root)
        with open(path, errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                kind = classify_line(line)
                if not kind:
                    continue
                ts = _line_ts(line, mtime)
                if ledger.older_than_retention(ts):
                    continue
                result["events"] += 1
                rows.append((
                    f"vscodelog:{rel}:{lineno}", ts, HARNESS, kind,
                    _mechanism_for(kind),
                    json.dumps({"line": line.strip()[:400]}),
                    rel.split(os.sep)[0],       # session dir = session id
                    SOURCE, f"{path}:{lineno}"))

    if rows and not dry_run:
        before = con.total_changes
        con.executemany(
            "INSERT OR IGNORE INTO event"
            " (uid, ts, harness, kind, mechanism, detail, session_id, source, raw_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.commit()
        result["inserted"] = con.total_changes - before
    if not dry_run:
        ledger.save_state(STATE_KEY, {"file_mtime": new_mtime})
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    force = "--force" in argv or dry_run
    con = ledger.connect()
    schema = os.path.join(os.path.dirname(__file__), "..", "schema_copilot_vscode.sql")
    if os.path.exists(schema):
        with open(schema) as f:
            con.executescript(f.read())
    r = collect(con, force=force, dry_run=dry_run)
    con.close()
    mode = "DRY RUN — nothing written" if dry_run else "harvested"
    print(f"[{STATE_KEY}] {mode}: {r['files']} files, {r['events']} events"
          f" (+{r['inserted']} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
