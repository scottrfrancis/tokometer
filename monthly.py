#!/usr/bin/env python3
"""Monthly rollover -- catch-up safe, idempotent.

Run from the daily job. It does NOT rely on firing at midnight on the 1st: every
run compares the calendar against a stored marker (state/last_monthly.json) and,
if the month has turned over since the last rollover, it:

  1. renders a month-scoped HTML report for each completed month not yet done,
  2. archives DB rows older than the 1st of last month to a .sql dump, then deletes
     them (keeps current + previous month live),
  3. deletes daily reports older than the 1st of last month,
  4. keeps only the last 12 monthly reports,
  5. records the rollover in the marker.

Because step (2) prunes data, reports in step (1) are always generated first, while
the data is still present. Re-runs within the same month are no-ops.
"""
import os
import sys
import glob
import json
import sqlite3
import datetime as dt

sys.path.insert(0, os.path.dirname(__file__))
import report_html as rh  # noqa: E402

TOKOMETER_HOME = rh.TOKOMETER_HOME
DB = rh.DB_PATH
REPORT_DIR = rh.REPORT_DIR
STATE = os.path.join(TOKOMETER_HOME, "state", "last_monthly.json")
ARCHIVE_DIR = os.path.join(TOKOMETER_HOME, "archive")
KEEP_MONTHLY = 12

# (table, timestamp column) for everything time-series.
PRUNE_TABLES = [("usage", "ts"), ("commit_metric", "ts"),
                ("pr_metric", "merged_at")]


def month_label(d):
    return f"{d.year:04d}-{d.month:02d}"


def first_of_prev_month(today):
    first_this = today.replace(day=1)
    return (first_this - dt.timedelta(days=1)).replace(day=1)


def load_marker():
    try:
        with open(STATE) as f:
            return json.load(f).get("last")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_marker(label):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(tmp, "w") as f:
        json.dump({"last": label, "ts": now}, f)
    os.replace(tmp, STATE)


def completed_months_with_data(con, current_label):
    rows = con.execute(
        "SELECT DISTINCT strftime('%Y-%m', ts, 'localtime') FROM usage"
    ).fetchall()
    return sorted(m for (m,) in rows if m and m < current_label)


def archive_and_prune(cutoff_iso):
    """Dump rows older than cutoff to a .sql file, then delete them."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    arch_db = os.path.join(ARCHIVE_DIR, f".tmp-{stamp}.db")
    sql_path = os.path.join(ARCHIVE_DIR, f"pruned-before-{cutoff_iso}-{stamp}.sql")

    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 30000;")
    con.execute("ATTACH ? AS arch", (arch_db,))
    moved = 0
    for tbl, col in PRUNE_TABLES:
        pred = f"date({col},'localtime') < ?"
        n = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {pred}", (cutoff_iso,)).fetchone()[0]
        if n:
            con.execute(f"CREATE TABLE arch.{tbl} AS SELECT * FROM {tbl} WHERE {pred}",
                        (cutoff_iso,))
            moved += n
    con.commit()
    con.execute("DETACH arch")
    con.close()

    # cursor_repo_hour buckets are 'YYYY-MM-DDTHH' local strings -> lexical compare
    con = sqlite3.connect(DB)
    con.execute("PRAGMA busy_timeout = 30000;")
    con.execute("DELETE FROM cursor_repo_hour WHERE hour < ?", (cutoff_iso,))
    con.commit()
    con.close()

    if moved:
        a = sqlite3.connect(arch_db)
        with open(sql_path, "w") as f:
            for line in a.iterdump():
                f.write(line + "\n")
        a.close()
        con = sqlite3.connect(DB)
        con.execute("PRAGMA busy_timeout = 30000;")
        for tbl, col in PRUNE_TABLES:
            con.execute(f"DELETE FROM {tbl} WHERE date({col},'localtime') < ?", (cutoff_iso,))
        con.commit()
        con.execute("VACUUM")
        con.close()
        print(f"[monthly] archived {moved} rows -> {sql_path}, pruned & vacuumed", file=sys.stderr)
    else:
        print("[monthly] nothing older than cutoff to prune", file=sys.stderr)
    if os.path.exists(arch_db):
        os.remove(arch_db)


def prune_daily_reports(cutoff):
    removed = 0
    for p in glob.glob(os.path.join(REPORT_DIR, "morning-*.html")):
        base = os.path.basename(p)[len("morning-"):-len(".html")]
        try:
            d = dt.date.fromisoformat(base)
        except ValueError:
            continue
        if d < cutoff:
            os.remove(p)
            removed += 1
    if removed:
        print(f"[monthly] deleted {removed} daily reports older than {cutoff}", file=sys.stderr)


def prune_monthly_reports(keep=KEEP_MONTHLY):
    months = sorted(glob.glob(os.path.join(REPORT_DIR, "month-*.html")))
    for p in months[:-keep] if len(months) > keep else []:
        os.remove(p)
        print(f"[monthly] aged out {os.path.basename(p)}", file=sys.stderr)


def rollover():
    today = dt.date.today()
    current = month_label(today)
    target = month_label(first_of_prev_month(today))   # previous completed month
    marker = load_marker()

    if marker == target:
        print(f"[monthly] already rolled over through {target}; nothing to do", file=sys.stderr)
        return

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    pending = [m for m in completed_months_with_data(con, current)
               if marker is None or m > marker]
    con.close()

    for m in pending:
        path = rh.generate(m)
        print(f"[monthly] wrote {path}", file=sys.stderr)

    cutoff = first_of_prev_month(today)            # keep current + previous month
    archive_and_prune(cutoff.isoformat())
    prune_daily_reports(cutoff)
    prune_monthly_reports()
    save_marker(target)
    print(f"[monthly] rollover complete; marker set to {target}", file=sys.stderr)


if __name__ == "__main__":
    rollover()
