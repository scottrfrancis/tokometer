#!/usr/bin/env python3
"""Fleet report: the morning report over every machine, built on the hub.

Every machine pushes its ledger rows to Beaufort's bronze.tokometer_usage.
fleet.py reads them back (read-only Postgres role), loads them into an
in-memory ledger with the usual columns plus host, arrival time and build,
renders report_html's report over all of them with a Machines table, and
writes for Morning Kata a summary whose lines kata_tokens.py prints verbatim.

Outputs, in $TOKOMETER_HOME/reports: fleet-<date>.html, fleet-latest.html
(the copy Studio fetches and opens) and fleet-summary.json.

Period: month to date, or from FLEET_SINCE when that is later (the fleet
count starts on the day every machine runs the same build). Rows with source
"heartbeat" (see push.client.heartbeat_row) are not usage: they only tell
when a machine was last heard from and which build it runs.

Flags, shown in the page and in Kata:
  * a machine in FLEET_HOSTS Beaufort has not heard from for more than 26 h;
  * a machine on a different build from the hub's own.

Config (fleet.env, sourced by fleet.sh): FLEET_PG_HOST, FLEET_PG_PORT,
FLEET_PG_DB, FLEET_PG_USER, FLEET_PG_PASSWORD, FLEET_SINCE (YYYY-MM-DD),
FLEET_HOSTS (space-separated). Needs psycopg2, imported only to fetch.
"""
from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import os
import sqlite3
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lib import pricing  # noqa: E402
import report_html as rh  # noqa: E402

UTC = dt.timezone.utc
STALE_AFTER = dt.timedelta(hours=26)
FETCH_DAYS_BEFORE = 8        # claude_limits looks back 7 days

DDL = """
CREATE TABLE usage (
  id INTEGER PRIMARY KEY, uid TEXT UNIQUE, ts TEXT NOT NULL, harness TEXT NOT NULL,
  provider TEXT, model TEXT, session_id TEXT, request_id TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0, credits INTEGER DEFAULT 0,
  total_tokens INTEGER GENERATED ALWAYS AS
    (input_tokens+output_tokens+cache_read_tokens+cache_write_tokens+reasoning_tokens) STORED,
  cost_usd REAL DEFAULT 0.0, source TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'exact',
  raw_ref TEXT, cwd TEXT, account TEXT, subscription TEXT, org TEXT,
  host TEXT NOT NULL, arrived_at TEXT, build TEXT);
CREATE TABLE heartbeat (host TEXT NOT NULL, ts TEXT, arrived_at TEXT, build TEXT);
"""
USAGE_INSERT = (
    "INSERT OR IGNORE INTO usage (uid, ts, harness, provider, model, session_id, input_tokens,"
    " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, credits, cost_usd,"
    " source, confidence, cwd, account, subscription, org, host, arrived_at, build)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, str):
        t = dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _when(iso: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def load(records: Iterable[dict]) -> sqlite3.Connection:
    """Bronze rows -> an in-memory ledger: usage rows plus a heartbeat table."""
    con = sqlite3.connect(":memory:")
    con.executescript(DDL)
    for r in records:
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        build = meta.get("tokometer_version")
        if meta.get("source") == "heartbeat":
            con.execute("INSERT INTO heartbeat VALUES (?,?,?,?)",
                        (r["host"], _iso(r["ts"]), _iso(r.get("arrived_at")), build))
            continue
        con.execute(USAGE_INSERT, (
            r["uid"], _iso(r["ts"]), r["harness"], r.get("provider") or meta.get("provider"),
            r.get("model"), r.get("session_id"),
            int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0),
            int(r.get("cache_read") or 0), int(r.get("cache_write") or 0),
            int(meta.get("reasoning_tokens") or 0), int(meta.get("credits") or 0),
            float(r.get("cost_usd") or 0), meta.get("source") or "unknown",
            meta.get("confidence") or "exact", r.get("cwd"), r.get("account"),
            meta.get("subscription"), meta.get("org"), r["host"], _iso(r.get("arrived_at")),
            build))
    con.commit()
    pricing.register(con)
    return con


def kmoney(v: float | None) -> str:
    """Compact dollars for a chat line: $0.19, $8.19, $86, $1.9K."""
    v = v or 0.0
    if v >= 1000:
        return f"${v / 1000:.1f}K"
    if v >= 10:
        return f"${v:,.0f}"
    return f"${v:.2f}"


def _account_label(org: str | None, sub: str | None) -> str:
    org = org or "unattributed"
    return f"{org} {sub}" if sub and sub != "self-hosted" else org


@dataclass
class Machine:
    host: str
    last_activity: dt.datetime | None
    last_seen: dt.datetime | None
    build: str | None
    tokens_yesterday: int
    value_yesterday: float | None
    value_period: float | None
    flags: list[str] = field(default_factory=list)


class Report:
    def __init__(self, con: sqlite3.Connection, *, expected: list[str], since: dt.date,
                 now: dt.datetime, hub_build: str | None,
                 last_seen: dict[str, dt.datetime]):
        self.con, self.expected, self.since, self.now = con, expected, since, now
        self.hub_build, self.db_last_seen = hub_build, last_seen
        self.today = now.date()
        self.yesterday = self.today - dt.timedelta(days=1)
        self.start = self.period_start(self.today, since)
        self.label = ("month-to-date" if self.start == self.today.replace(day=1)
                      else f"since {since:%b %-d}")

    @staticmethod
    def period_start(today: dt.date, since: dt.date) -> dt.date:
        return max(today.replace(day=1), since)

    def _one(self, sql: str, params: tuple = ()):
        return self.con.execute(sql, params).fetchone()

    # ---- per machine

    def _last_seen(self, host: str) -> dt.datetime | None:
        (a,) = self._one("SELECT MAX(t) FROM (SELECT arrived_at t FROM usage WHERE host=? "
                         "UNION ALL SELECT arrived_at FROM heartbeat WHERE host=?)", (host, host))
        seen = [x for x in (_when(a), self.db_last_seen.get(host)) if x]
        return max(seen) if seen else None

    def _build(self, host: str) -> str | None:
        r = self._one("SELECT build FROM (SELECT build, arrived_at FROM usage WHERE host=? "
                      "UNION ALL SELECT build, arrived_at FROM heartbeat WHERE host=?) "
                      "WHERE build IS NOT NULL ORDER BY arrived_at DESC LIMIT 1", (host, host))
        return r[0] if r else None

    def _since_text(self, seen: dt.datetime) -> str:
        local = seen.astimezone(self.now.tzinfo)
        return f"{local:%a}" if self.now - seen < dt.timedelta(days=7) else f"{local:%b %-d}"

    def machines(self) -> list[Machine]:
        seen_hosts = {r[0] for r in self.con.execute(
            "SELECT host FROM usage UNION SELECT host FROM heartbeat")}
        hosts = list(self.expected) + sorted(seen_hosts - set(self.expected))
        out = []
        for h in hosts:
            (act,) = self._one("SELECT MAX(ts) FROM usage WHERE host=?", (h,))
            tok_y, val_y = self._one(
                f"SELECT COALESCE(SUM(total_tokens),0), {pricing.SUM_VALUE} FROM usage "
                "WHERE host=? AND date(ts,'localtime') = ?", (h, self.yesterday.isoformat()))
            (val_p,) = self._one(f"SELECT {pricing.SUM_VALUE} FROM usage WHERE host=? "
                                 "AND date(ts,'localtime') >= ?", (h, self.start.isoformat()))
            m = Machine(h, _when(act), self._last_seen(h), self._build(h), tok_y, val_y, val_p)
            if m.last_seen is None:
                m.flags.append("no data yet")
            elif self.now - m.last_seen > STALE_AFTER:
                m.flags.append(f"no data since {self._since_text(m.last_seen)}")
            # A build that differs from the hub's, or recent rows that name none
            # (a client older than the build stamp). A machine with nothing in
            # the window has no known build, which is not a build problem.
            recent = h in seen_hosts
            if self.hub_build and recent and m.build != self.hub_build:
                m.flags.append(f"build {m.build or 'unknown'}, hub runs {self.hub_build}")
            out.append(m)
        return out

    # ---- summary for Kata

    def summary(self) -> dict:
        tok, val, n = self._one(
            f"SELECT COALESCE(SUM(total_tokens),0), {pricing.SUM_VALUE}, "
            "COUNT(DISTINCT CASE WHEN total_tokens > 0 THEN host END) FROM usage "
            "WHERE date(ts,'localtime') = ?", (self.yesterday.isoformat(),))
        by: dict[str, float] = {}
        for org, sub, v in self.con.execute(
                f"SELECT org, subscription, {pricing.SUM_VALUE} FROM usage "
                "WHERE date(ts,'localtime') >= ? GROUP BY org, subscription",
                (self.start.isoformat(),)):
            if v:
                label = _account_label(org, sub)
                by[label] = by.get(label, 0.0) + v
        by_account = sorted(([k, v] for k, v in by.items()), key=lambda kv: -kv[1])
        period_value = sum(v for _, v in by_account)
        machines = self.machines()
        flags = ([f"{m.host}: {f}" for m in machines for f in m.flags if f.startswith("no data")]
                 + [f"{m.host}: {f}" for m in machines for f in m.flags
                    if not f.startswith("no data")])

        if self.yesterday < self.since:
            when = f"today, {self.since:%b %-d}" if self.since == self.today else f"{self.since:%b %-d}"
            lines = [f":bar_chart: Tokens: the all-machines count starts {when}"]
        else:
            lead = "MTD" if self.label == "month-to-date" else f"Since {self.since:%b %-d}"
            lines = [
                f":bar_chart: Tokens yesterday: {rh.human(tok)} on {n} machine"
                f"{'' if n == 1 else 's'}, {kmoney(val)} list value",
                f"   {lead} {kmoney(period_value)}: "
                + " · ".join(f"{k} {kmoney(v)}" for k, v in by_account[:4]),
            ]
        lines += [f":warning: {f}" for f in flags]
        return {
            "date": self.today.isoformat(),
            "generated_at": self.now.isoformat(timespec="seconds"),
            "since": self.since.isoformat(),
            "yesterday": {"date": self.yesterday.isoformat(), "tokens": tok, "value": val,
                          "machines": n},
            "period": {"start": self.start.isoformat(), "label": self.label,
                       "value": period_value, "by_account": by_account},
            "machines": [{"host": m.host, "last_seen": _iso(m.last_seen), "build": m.build,
                          "flags": m.flags} for m in machines],
            "flags": flags,
            "kata_lines": lines,
        }

    # ---- the page

    def machines_html(self, machines: list[Machine]) -> str:
        def when(t):
            return f"{t.astimezone(self.now.tzinfo):%a %b %-d %H:%M}" if t else "never"
        trs = "".join(
            f"<tr><td>{htmllib.escape(m.host)}</td><td>{when(m.last_activity)}</td>"
            f"<td>{when(m.last_seen)}</td><td>{htmllib.escape(m.build or '?')}</td>"
            f'<td class="r">{rh.human(m.tokens_yesterday)}</td>'
            f'<td class="r">{rh.money(m.value_yesterday)}</td>'
            f'<td class="r">{rh.money(m.value_period)}</td>'
            f"<td>{htmllib.escape('; '.join(m.flags))}</td></tr>" for m in machines)
        return ('<table class="stats"><thead><tr><th>machine</th><th>last activity</th>'
                "<th>last heard from</th><th>build</th><th>tokens yesterday</th>"
                f"<th>value yesterday</th><th>value ({self.label})</th><th>flags</th>"
                f"</tr></thead><tbody>{trs}</tbody></table>")

    def html(self) -> str:
        machines = self.machines()
        flags = [f"{m.host}: {f}" for m in machines for f in m.flags]
        flags_html = ("" if not flags else
                      '<div style="background:#fff4e5;border:1px solid #f59f00;border-radius:6px;'
                      'padding:8px 12px;margin:10px 0">'
                      + "<br>".join(htmllib.escape(f) for f in flags) + "</div>")
        # report_html keeps its period in module globals; put them back after.
        saved = (rh.PERIOD_START, rh.PERIOD_END, rh.IS_MONTH, rh.PERIOD_LABEL)
        rh.set_window(self.start, self.label)
        try:
            return rh.build_fleet(self.con, self.machines_html(machines), flags_html)
        finally:
            rh.PERIOD_START, rh.PERIOD_END, rh.IS_MONTH, rh.PERIOD_LABEL = saved

    def write(self, out_dir: Path) -> dict[str, Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        page = self.html()
        paths = {"html": out_dir / f"fleet-{self.today.isoformat()}.html",
                 "latest": out_dir / "fleet-latest.html",
                 "summary": out_dir / "fleet-summary.json"}
        for key in ("html", "latest"):
            tmp = paths[key].with_suffix(".tmp")
            tmp.write_text(page)
            tmp.replace(paths[key])
        tmp = paths["summary"].with_suffix(".tmp")
        tmp.write_text(json.dumps(self.summary(), indent=2))
        tmp.replace(paths["summary"])
        return paths


# ---- Postgres (only on the hub)

def fetch(from_day: dt.date) -> tuple[list[dict], dict[str, dt.datetime]]:
    """Bronze rows from local midnight of from_day, and every host's last arrival."""
    import psycopg2
    import psycopg2.extras
    env = os.environ
    con = psycopg2.connect(host=env["FLEET_PG_HOST"], port=int(env.get("FLEET_PG_PORT", "5432")),
                           dbname=env["FLEET_PG_DB"], user=env["FLEET_PG_USER"],
                           password=env["FLEET_PG_PASSWORD"], connect_timeout=20)
    try:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        start = dt.datetime.combine(from_day, dt.time()).astimezone()
        cur.execute(
            "SELECT uid, host, ts, harness, COALESCE(provider, meta->>'provider') AS provider,"
            " model, account, session_id, cwd, input_tokens, output_tokens, cache_read,"
            " cache_write, cost_usd, meta, arrived_at"
            " FROM bronze.tokometer_usage WHERE ts >= %s", (start,))
        records = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT host, MAX(arrived_at) AS seen FROM bronze.tokometer_usage GROUP BY host")
        last_seen = {r["host"]: r["seen"] for r in cur.fetchall()}
    finally:
        con.close()
    return records, last_seen


def main() -> int:
    os.environ.pop("TOKOMETER_HARNESSES", None)   # the hub's own harness list is not the fleet's
    home = Path(os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer")))
    since = dt.date.fromisoformat(os.environ["FLEET_SINCE"])
    expected = os.environ.get("FLEET_HOSTS", "").split()
    try:
        hub_build = (home / "VERSION").read_text().strip() or None
    except OSError:
        hub_build = None
    now = dt.datetime.now().astimezone()
    start = Report.period_start(now.date(), since)
    records, last_seen = fetch(min(start, now.date() - dt.timedelta(days=FETCH_DAYS_BEFORE)))
    report = Report(load(records), expected=expected, since=since, now=now,
                    hub_build=hub_build, last_seen=last_seen)
    paths = report.write(home / "reports")
    print(paths["html"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
