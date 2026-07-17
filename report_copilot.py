#!/usr/bin/env python3
"""Copilot-on-VS-Code strategy report — daily and weekly, for the locked-down box.

Answers the questions the harness exists for:
  which models did Auto actually pick, when — model mix by hour, by day;
  what failed and why — crash/stall/quota/power events with mechanism labels;
  is it time-of-day — the hourly mix IS the Experiment-A instrument;
  what did it feel like — manual quality ratings alongside the hard numbers.

Usage:
    python3 report_copilot.py               # today (local)
    python3 report_copilot.py --weekly      # last 7 days
    python3 report_copilot.py --date 2026-07-15
    python3 report_copilot.py --since 2026-07-10 --until 2026-07-16

Markdown to stdout; also written to $TOKOMETER_HOME/reports/copilot-<range>.md.
All buckets use LOCAL time (the whole point is *your* working hours).
Local-only; stdlib-only; Python 3.11-compatible.
"""
import os
import sys
import json
import sqlite3
import argparse
import datetime as dt

TOKOMETER_HOME = os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer"))

WHERE_RANGE = "date(ts,'localtime') BETWEEN ? AND ?"
USAGE_FILTER = "harness='copilot' AND source IN ('vscode-chat-log','otel-file')"

EVENT_ORDER = [
    "worker_oom", "v8_oom", "exthost_crash", "exthost_restart", "listener_leak",
    "request_failure", "slow_request", "stall", "continue_prompt",
    "power_throttle", "quota", "context_budget", "tool_result_disk",
]


def _connect():
    con = sqlite3.connect(os.path.join(TOKOMETER_HOME, "ledger.db"), timeout=30)
    con.execute("PRAGMA busy_timeout = 30000;")
    return con


def human(n):
    n = n or 0
    for unit, div in (("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))


def model_mix(con, since, until):
    """Per-model requests + tokens in the local-date range, most-used first."""
    rows = con.execute(
        f"SELECT model, COUNT(*) requests,"
        f" SUM(input_tokens), SUM(output_tokens),"
        f" SUM(cache_read_tokens), SUM(cache_write_tokens),"
        f" SUM(CASE WHEN confidence='estimate' THEN 1 ELSE 0 END) estimated"
        f" FROM usage WHERE {USAGE_FILTER} AND {WHERE_RANGE}"
        f" GROUP BY model ORDER BY requests DESC", (since, until)).fetchall()
    return [{"model": r[0] or "(unknown)", "requests": r[1],
             "input_tokens": r[2] or 0, "output_tokens": r[3] or 0,
             "cache_read_tokens": r[4] or 0, "cache_write_tokens": r[5] or 0,
             "estimated": r[6] or 0} for r in rows]


def hourly_mix(con, since, until):
    """Per local-hour request counts with the dominant model — the Exp-A table."""
    rows = con.execute(
        f"SELECT strftime('%H', ts, 'localtime') hour, COUNT(*) requests,"
        f" model, SUM(output_tokens)"
        f" FROM usage WHERE {USAGE_FILTER} AND {WHERE_RANGE}"
        f" GROUP BY hour, model ORDER BY hour", (since, until)).fetchall()
    by_hour = {}
    for hour, requests, model, out_tok in rows:
        h = by_hour.setdefault(hour, {"hour": hour, "requests": 0,
                                      "output_tokens": 0, "models": {}})
        h["requests"] += requests
        h["output_tokens"] += out_tok or 0
        h["models"][model or "(unknown)"] = \
            h["models"].get(model or "(unknown)", 0) + requests
    out = []
    for hour in sorted(by_hour):
        h = by_hour[hour]
        h["top_model"] = max(h["models"], key=h["models"].get)
        out.append(h)
    return out


def event_summary(con, since, until):
    """Event counts by kind, merging harvested events with manual observations."""
    counts = {}
    for kind, n in con.execute(
            f"SELECT kind, COUNT(*) FROM event"
            f" WHERE kind != 'model_downgrade' AND {WHERE_RANGE}"
            f" GROUP BY kind", (since, until)):
        counts[kind] = n
    for kind, n in con.execute(
            f"SELECT kind, COUNT(*) FROM manual_obs"
            f" WHERE kind != 'rating' AND {WHERE_RANGE}"
            f" GROUP BY kind", (since, until)):
        counts[kind] = counts.get(kind, 0) + n
    return counts


def downgrades(con, since, until):
    rows = con.execute(
        f"SELECT ts, mechanism, detail FROM event"
        f" WHERE kind='model_downgrade' AND {WHERE_RANGE} ORDER BY ts",
        (since, until)).fetchall()
    out = []
    for ts, mechanism, detail in rows:
        d = {}
        try:
            d = json.loads(detail or "{}")
        except json.JSONDecodeError:
            pass
        out.append({"ts": ts, "mechanism": mechanism or "unlabeled",
                    "from": d.get("from"), "to": d.get("to")})
    return out


def quality(con, since, until):
    row = con.execute(
        f"SELECT COUNT(*), AVG(quality) FROM manual_obs"
        f" WHERE kind='rating' AND quality IS NOT NULL AND {WHERE_RANGE}",
        (since, until)).fetchone()
    return {"ratings": row[0] or 0, "avg": round(row[1], 2) if row[1] else None}


def slow_requests(con, since, until, limit=10):
    rows = con.execute(
        f"SELECT ts, detail FROM event"
        f" WHERE kind='slow_request' AND {WHERE_RANGE} ORDER BY ts DESC LIMIT ?",
        (since, until, limit)).fetchall()
    out = []
    for ts, detail in rows:
        try:
            d = json.loads(detail or "{}")
        except json.JSONDecodeError:
            d = {}
        out.append({"ts": ts, "latency_ms": d.get("latency_ms"),
                    "model": d.get("model"), "origin": d.get("origin")})
    return out


def render(con, since, until, title):
    mix = model_mix(con, since, until)
    lines = [f"# {title} ({since}" + (f" → {until}" if until != since else "") + ")", ""]
    if not mix:
        lines.append("_No Copilot activity harvested in this range."
                     " Is the chat log level still set to Trace?_")
        return "\n".join(lines)

    total_req = sum(m["requests"] for m in mix)
    total_out = sum(m["output_tokens"] for m in mix)
    est = sum(m["estimated"] for m in mix)
    lines += [
        f"**{total_req} requests**, {human(total_out)} output tokens"
        + (f" ({est} request(s) without token data — estimates)" if est else ""),
        "",
        "## Model mix",
        "",
        "| model | requests | share | out tokens | cache read | est? |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for m in mix:
        lines.append(
            f"| {m['model']} | {m['requests']} | {m['requests']*100//total_req}% "
            f"| {human(m['output_tokens'])} | {human(m['cache_read_tokens'])} "
            f"| {m['estimated'] or ''} |")

    hours = hourly_mix(con, since, until)
    if hours:
        lines += ["", "## By hour (local) — the time-of-day instrument", "",
                  "| hour | requests | top model | out tokens |", "|---|---:|---|---:|"]
        for h in hours:
            lines.append(f"| {h['hour']}:00 | {h['requests']} | {h['top_model']} "
                         f"| {human(h['output_tokens'])} |")

    ev = event_summary(con, since, until)
    lines += ["", "## Health & failure events", ""]
    if ev:
        lines += ["| event | count |", "|---|---:|"]
        for kind in EVENT_ORDER:
            if kind in ev:
                lines.append(f"| {kind} | {ev[kind]} |")
        for kind in sorted(set(ev) - set(EVENT_ORDER)):
            lines.append(f"| {kind} | {ev[kind]} |")
    else:
        lines.append("_none recorded_")

    dg = downgrades(con, since, until)
    lines += ["", "## Downgrades (classifier)", ""]
    if dg:
        lines += ["| when | from → to | mechanism |", "|---|---|---|"]
        for d in dg:
            lines.append(f"| {d['ts']} | {d['from']} → {d['to']} | {d['mechanism']} |")
    else:
        lines.append("_none detected_")

    slow = slow_requests(con, since, until)
    if slow:
        lines += ["", "## Slow requests (stall-shaped, ≥30s)", "",
                  "| when | latency | model | origin |", "|---|---:|---|---|"]
        for s in slow:
            secs = f"{(s['latency_ms'] or 0)/1000:.0f}s"
            lines.append(f"| {s['ts']} | {secs} | {s['model'] or '?'} "
                         f"| {s['origin'] or '?'} |")

    q = quality(con, since, until)
    if q["ratings"]:
        lines += ["", f"**Quality (manual):** {q['ratings']} rating(s), avg {q['avg']}/5"]

    lines += ["", "---",
              "_Every number is local to this machine; nothing leaves the box._"]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="Copilot daily/weekly strategy report")
    p.add_argument("--weekly", action="store_true", help="last 7 days")
    p.add_argument("--date", help="single day YYYY-MM-DD (default: today)")
    p.add_argument("--since", help="range start YYYY-MM-DD")
    p.add_argument("--until", help="range end YYYY-MM-DD")
    args = p.parse_args(argv)

    today = dt.date.today().isoformat()
    if args.since:
        since, until = args.since, args.until or today
        title, slug = "Copilot report", f"{since}_{until}"
    elif args.weekly:
        since = (dt.date.today() - dt.timedelta(days=6)).isoformat()
        until, title, slug = today, "Copilot weekly", f"weekly-{today}"
    else:
        since = until = args.date or today
        title, slug = "Copilot daily", since

    con = _connect()
    md = render(con, since, until, title)
    con.close()
    print(md)
    report_dir = os.path.join(TOKOMETER_HOME, "reports")
    os.makedirs(report_dir, exist_ok=True)
    out_path = os.path.join(report_dir, f"copilot-{slug}.md")
    with open(out_path, "w") as f:
        f.write(md + "\n")
    print(f"\n[report_copilot] written to {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
