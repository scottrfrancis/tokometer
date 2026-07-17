#!/usr/bin/env python3
"""GitHub Copilot in VS Code harvester — Trace-level chat output channel.

PRIMARY Copilot source on VS-Code-only machines (no CLI). Reads the "GitHub
Copilot Chat" output-channel log under the VS Code session-logs tree:

    %APPDATA%/Code/logs/<YYYYMMDDTHHMMSS>/window*/exthost/output_logging_*/
        *GitHub Copilot Chat*.log

Verified line grammar (Copilot Chat 0.42.3 on VS Code 1.129.0, 2026-07-17;
log level must be Trace):

  - per-request summary (the ledger spine; one per model request):
      ccreq:<hex>.copilotmd | success | <alias> -> <deployment> | <ms>ms | [origin]
    (the "-> <deployment>" part is absent for some OpenAI-family requests)
  - token usage arrives on adjacent SSE lines BEFORE the ccreq line:
      Anthropic-style: {"usage":{"input_tokens":…,"output_tokens":…,
        "cache_creation_input_tokens":…,"cache_read_input_tokens":…}}
      Bedrock: {"amazon-bedrock-invocationMetrics":{"inputTokenCount":…,…,
        "invocationLatency":…}}   (Claude models are served via Bedrock)
      copilot_usage: {"copilot_usage":{…,"total_nano_aiu":N}} (billing units)
      OpenAI-style: {"usage":{"prompt_tokens":…,"completion_tokens":…}}
  - diagnostics (captured as event rows):
      [Power] CPU speed limit changed: N% (throttled)
      [ChatQuota] processQuotaHeaders: {…}
      [Agent] rendering with budget=N (baseBudget: N, toolTokens: N, totalTools: N…)
      [ToolResult] Large tool result (N bytes) written to disk: …
      [error]-level lines and "| failure |" ccreq lines → request_failure

Requests with no adjacent usage still land in the ledger (tokens 0, confidence
'estimate') — the request/model/latency facts are exact even when tokens are absent.
Timestamps are the box's local time; normalized to UTC here (collector runs on the
same box). Local-only; stdlib-only; Python 3.11-compatible.
"""
import os
import re
import sys
import json
import glob
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "copilot"
PROVIDER = "github"
SOURCE = "vscode-chat-log"
STATE_KEY = "copilot_chat_log"

_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
DEFAULT_GLOB = os.path.join(
    _APPDATA, "Code", "logs", "*", "window*", "exthost",
    "output_logging_*", "*GitHub Copilot Chat*.log")

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \[(\w+)\] (.*)$")
_CCREQ_RE = re.compile(
    r"ccreq:([0-9a-fA-F]+)\.copilotmd \| (\w+) \| (.+?) \| (\d+)ms \| \[([^\]]+)\]")
_POWER_RE = re.compile(r"\[Power\] CPU speed limit changed: (\d+)%")
_QUOTA_RE = re.compile(r"\[ChatQuota\] processQuotaHeaders: (\{.*\})")
_BUDGET_RE = re.compile(
    r"\[Agent\] rendering with budget=(\d+) \(baseBudget: (\d+), "
    r"toolTokens: (\d+), totalTools: (\d+)")
_TOOLRESULT_RE = re.compile(r"\[ToolResult\] Large tool result \((\d+) bytes\)")
_CONVO_RE = re.compile(r"New request for conversation ([0-9a-fA-F-]{36})")


def _to_iso_utc(local_ts):
    """'2026-07-17 08:17:18.084' (box-local) -> ISO-8601 UTC 'Z'."""
    t = dt.datetime.fromisoformat(local_ts).astimezone(dt.timezone.utc)
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ccreq(msg):
    """Parse a per-request summary line; None if the line isn't one."""
    m = _CCREQ_RE.search(msg)
    if not m:
        return None
    model_part = m.group(3)
    alias, sep, deployment = model_part.partition(" -> ")
    return {
        "ccreq": m.group(1).lower(),
        "status": m.group(2),
        "alias": alias.strip(),
        "deployment": deployment.strip() if sep else None,
        "latency_ms": int(m.group(4)),
        "origin": m.group(5),
    }


def extract_usage(payload):
    """Pull token counts out of an SSE JSON payload; None if it carries none.

    Handles Anthropic usage, OpenAI usage, Bedrock invocationMetrics, and the
    copilot_usage billing block — merged into one normalized dict.
    """
    try:
        d = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    out = {}
    u = d.get("usage")
    if isinstance(u, dict):
        if "input_tokens" in u or "output_tokens" in u:      # Anthropic
            out["input_tokens"] = int(u.get("input_tokens") or 0)
            out["output_tokens"] = int(u.get("output_tokens") or 0)
            out["cache_write_tokens"] = int(u.get("cache_creation_input_tokens") or 0)
            out["cache_read_tokens"] = int(u.get("cache_read_input_tokens") or 0)
        elif "prompt_tokens" in u or "completion_tokens" in u:  # OpenAI
            out["input_tokens"] = int(u.get("prompt_tokens") or 0)
            out["output_tokens"] = int(u.get("completion_tokens") or 0)
    bmetrics = d.get("amazon-bedrock-invocationMetrics")
    if isinstance(bmetrics, dict):
        out.setdefault("input_tokens", int(bmetrics.get("inputTokenCount") or 0))
        out.setdefault("output_tokens", int(bmetrics.get("outputTokenCount") or 0))
        out.setdefault("cache_read_tokens", int(bmetrics.get("cacheReadInputTokenCount") or 0))
        out.setdefault("cache_write_tokens", int(bmetrics.get("cacheWriteInputTokenCount") or 0))
        if bmetrics.get("invocationLatency") is not None:
            out["invocation_latency_ms"] = int(bmetrics["invocationLatency"])
    cu = d.get("copilot_usage")
    if isinstance(cu, dict) and cu.get("total_nano_aiu") is not None:
        out["nano_aiu"] = int(cu["total_nano_aiu"])
    return out or None


def _insert_events(con, rows):
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO event"
        " (uid, ts, harness, kind, mechanism, detail, session_id, source, raw_ref)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["uid"], r["ts"], HARNESS, r["kind"], r.get("mechanism"),
          r.get("detail"), r.get("session_id"), SOURCE, r.get("raw_ref"))
         for r in rows])
    con.commit()
    return con.total_changes - before


def _parse_file(path):
    """Parse one chat log file -> (usage_rows, event_rows)."""
    base = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(path))))) + "/" + os.path.basename(path)
    usage_rows, event_rows = [], []
    conversation = None
    pending = {}       # merged usage fragments awaiting their ccreq line

    def event(lineno, ts, kind, detail=None, mechanism=None):
        event_rows.append({
            "uid": f"cclog:{base}:{lineno}", "ts": ts, "kind": kind,
            "mechanism": mechanism,
            "detail": json.dumps(detail) if isinstance(detail, dict) else detail,
            "session_id": conversation, "raw_ref": f"{path}:{lineno}",
        })

    with open(path, errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            m = _TS_RE.match(line.rstrip("\n"))
            if not m:
                continue
            local_ts, level, msg = m.groups()
            ts = _to_iso_utc(local_ts)

            cm = _CONVO_RE.search(msg)
            if cm:
                conversation = cm.group(1)

            if '"usage"' in msg or "invocationMetrics" in msg or "copilot_usage" in msg:
                brace = msg.find("{")
                if brace != -1:
                    u = extract_usage(msg[brace:])
                    if u:
                        pending.update(u)

            req = parse_ccreq(msg)
            if req:
                has_tokens = "input_tokens" in pending or "output_tokens" in pending
                usage_rows.append({
                    "uid": f"copilot-vscode:{req['ccreq']}",
                    "ts": ts, "harness": HARNESS, "provider": PROVIDER,
                    "model": req["deployment"] or req["alias"],
                    "session_id": conversation, "request_id": req["ccreq"],
                    "input_tokens": pending.get("input_tokens", 0),
                    "output_tokens": pending.get("output_tokens", 0),
                    "cache_read_tokens": pending.get("cache_read_tokens", 0),
                    "cache_write_tokens": pending.get("cache_write_tokens", 0),
                    "credits": pending.get("nano_aiu", 0),
                    "source": SOURCE,
                    "confidence": "exact" if has_tokens else "estimate",
                    "raw_ref": f"{path}:{lineno}",
                })
                if req["status"] != "success":
                    event(lineno, ts, "request_failure",
                          {"ccreq": req["ccreq"], "status": req["status"],
                           "latency_ms": req["latency_ms"], "origin": req["origin"]})
                pending = {}
                continue

            pm = _POWER_RE.search(msg)
            if pm:
                event(lineno, ts, "power_throttle", {"percent": int(pm.group(1))})
                continue
            qm = _QUOTA_RE.search(msg)
            if qm:
                event(lineno, ts, "quota", qm.group(1))
                continue
            bm = _BUDGET_RE.search(msg)
            if bm:
                event(lineno, ts, "context_budget",
                      {"budget": int(bm.group(1)), "base_budget": int(bm.group(2)),
                       "tool_tokens": int(bm.group(3)), "total_tools": int(bm.group(4))})
                continue
            tm = _TOOLRESULT_RE.search(msg)
            if tm:
                event(lineno, ts, "tool_result_disk", {"bytes": int(tm.group(1))})
                continue
            if level == "error":
                event(lineno, ts, "request_failure", {"message": msg[:400]})
    return usage_rows, event_rows


def collect(con, log_glob=None, force=False, dry_run=False):
    pattern = log_glob or os.environ.get("COPILOT_CHAT_LOG_GLOB", DEFAULT_GLOB)
    files = sorted(glob.glob(pattern))
    result = {"files": 0, "requests": 0, "events": 0,
              "inserted_usage": 0, "inserted_events": 0}
    if not files:
        print(f"[{STATE_KEY}] no Copilot Chat logs match {pattern}; skipping"
              " (is the log level set to Trace?)", file=sys.stderr)
        return result

    state = ledger.load_state(STATE_KEY)
    seen_mtime = {} if force else state.get("file_mtime", {})
    new_mtime = dict(state.get("file_mtime", {}))

    for path in files:
        mtime = os.path.getmtime(path)
        if seen_mtime.get(path) == mtime:
            continue
        new_mtime[path] = mtime
        usage_rows, event_rows = _parse_file(path)
        usage_rows = [r for r in usage_rows if not ledger.older_than_retention(r["ts"])]
        event_rows = [r for r in event_rows if not ledger.older_than_retention(r["ts"])]
        result["files"] += 1
        result["requests"] += len(usage_rows)
        result["events"] += len(event_rows)
        if not dry_run:
            result["inserted_usage"] += ledger.insert_usage(con, usage_rows)
            result["inserted_events"] += _insert_events(con, event_rows)

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
    print(f"[{STATE_KEY}] {mode}: {r['files']} files, {r['requests']} requests"
          f" (+{r['inserted_usage']} new), {r['events']} events"
          f" (+{r['inserted_events']} new)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
