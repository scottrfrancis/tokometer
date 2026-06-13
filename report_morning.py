#!/usr/bin/env python3
"""Morning report: month-to-date usage from the local tokometer ledger.

Sections:
  0. Claude limits -- rolling 5h/daily/weekly (+Opus weekly) usage per account.
  1. Headline  -- MTD (1st of month -> today, local) totals by harness, ranked by
                  total_tokens, with per-model subtotals. Account-aware harnesses
                  (claude-code) are split per account, e.g. "claude-code · Employer".
  2. Daily     -- per day: harness totals + that harness's top model only.
  3. Hotspots  -- day-of-week AND day-part (night/morning/afternoon/evening).
  4. Accounts  -- usage by account / subscription (friendly org label).
  5. Repos x accounts -- per-repo token split across accounts; flags repos touched
                  by more than one account (IP-isolation eyeball check).
Account/repo sections sit at the end; the headline already carries the per-account split.

Renders Markdown to stdout and also writes it to
  $TOKOMETER_HOME/reports/morning-YYYY-MM-DD.md
All buckets use LOCAL time. exact vs estimate is shown so coarse numbers
(Copilot/Cursor) aren't over-trusted.
"""
import os
import sys
import json
import sqlite3
import datetime as dt

TOKOMETER_HOME = os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer"))
DB_PATH = os.path.join(TOKOMETER_HOME, "ledger.db")
REPORT_DIR = os.path.join(TOKOMETER_HOME, "reports")
CURSOR_FETCH_STATE = os.path.join(TOKOMETER_HOME, "state", "cursor_fetch.json")

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]  # %w: 0=Sun..6=Sat
DAYPARTS = [("night", 0, 6), ("morning", 6, 12), ("afternoon", 12, 18), ("evening", 18, 24)]

# ts is ISO-UTC 'Z'; bucket in local time, from the 1st of the month to now
WHERE_MTD = "WHERE date(ts,'localtime') >= date('now','localtime','start of month')"

# A "display harness": account-aware harnesses (claude-code) are split per org so
# each account pair reads as its own first-level harness.
HKEY = ("(CASE WHEN org IS NOT NULL AND TRIM(COALESCE(org,'')) <> '' "
        "THEN harness || ' · ' || org ELSE harness END)")


def enabled_harnesses():
    """Set of harness names to surface, from TOKOMETER_HARNESSES; None = all enabled."""
    v = os.environ.get("TOKOMETER_HARNESSES")
    return set(v.split()) if v and v.strip() else None


def human(n):
    n = n or 0
    for unit, div in (("M", 1_000_000), ("K", 1_000)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))


def usd(x):
    return "$" + format(x or 0, ".2f")


def q(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def bar(value, peak, width=20):
    if not value or not peak:
        return ""
    return "#" * max(1, int(round(width * value / peak)))


def advisory(con):
    """Surface Cursor auto-fetch failures and stale data at the top of the report."""
    en = enabled_harnesses()
    if en is not None and "cursor" not in en:
        return ""   # cursor disabled on this machine; no Cursor advisory
    notes = []
    fetch_ok = None
    try:
        with open(CURSOR_FETCH_STATE) as f:
            st = json.load(f)
        fetch_ok = st.get("ok")
        if not fetch_ok:
            notes.append(
                f"Cursor auto-fetch FAILED: {st.get('error', 'unknown')} "
                f"(at {st.get('ts', '?')}). Re-auth with "
                "`python3 ~/.tokometer/collectors/cursor_fetch.py --login`, "
                "then `python3 ~/.tokometer/collectors/cursor_fetch.py`."
            )
    except (FileNotFoundError, json.JSONDecodeError):
        notes.append(
            "Cursor auto-fetch has never run. Set it up once: "
            "`python3 ~/.tokometer/collectors/cursor_fetch.py --login`."
        )

    latest = con.execute(
        "SELECT MAX(date(ts,'localtime')) FROM usage WHERE harness='cursor'"
    ).fetchone()[0]
    if latest:
        gap = (dt.date.today() - dt.date.fromisoformat(latest)).days
        if gap >= 2:
            notes.append(f"Cursor data is {gap} days stale (latest activity {latest}); "
                         "the last fetch may not have refreshed the CSV.")
    elif fetch_ok:
        notes.append("Cursor fetch reported success but no Cursor rows are in the ledger.")

    if not notes:
        return ""
    return "> [!WARNING] Advisories\n" + "\n".join(f"> - {n}" for n in notes) + "\n"


def claude_limits(con):
    """Rolling-window Claude usage per account (5h / daily / weekly + Opus weekly).

    Anthropic doesn't publish fixed thresholds and Claude Code stores no live limit
    consumption locally, so this is YOUR usage per window from the ledger (a burn-rate
    gauge), not a literal % of limit.
    """
    en = enabled_harnesses()
    if en is not None and "claude_code" not in en:
        return ""
    now = dt.datetime.now(dt.timezone.utc)

    def cutoff(**kw):
        return (now - dt.timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")

    windows = [("5-hour", cutoff(hours=5), False), ("daily", cutoff(days=1), False),
               ("weekly", cutoff(days=7), False), ("Opus wk", cutoff(days=7), True)]
    accts = [r[0] for r in q(con, """
        SELECT COALESCE(NULLIF(org,''),'(unattributed)') org
        FROM usage WHERE harness='claude-code' GROUP BY org
        ORDER BY SUM(total_tokens) DESC""")]
    if not accts:
        return ""
    L = ["## Claude limits -- rolling windows (messages · tokens)", "",
         "| account | " + " | ".join(w[0] for w in windows) + " |",
         "|---|" + "--:|" * len(windows)]
    for acct in accts:
        cells = []
        for _, since, opus in windows:
            opus_f = "AND model LIKE '%opus%'" if opus else ""
            msgs, tok = q(con, f"""SELECT COUNT(*), COALESCE(SUM(total_tokens),0)
                FROM usage WHERE harness='claude-code'
                  AND COALESCE(NULLIF(org,''),'(unattributed)')=? AND ts>=? {opus_f}""",
                          (acct, since))[0]
            cells.append("·" if not msgs else f"{msgs} · {human(tok)}")
        L.append(f"| {acct} | " + " | ".join(cells) + " |")
    L += ["", "_Your usage per window, not a published limit (Anthropic's thresholds are "
          "dynamic; Claude Code keeps no local limit counter)._", ""]
    return "\n".join(L)


def headline(con):
    L = [f"## Headline -- {dt.date.today().replace(day=1)} -> {dt.date.today()} (month-to-date)", ""]
    rows = q(con, f"""
        SELECT {HKEY} hk, MAX(confidence) conf,
               SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
               SUM(reasoning_tokens), SUM(total_tokens), SUM(cost_usd), COUNT(*)
        FROM usage {WHERE_MTD}
        GROUP BY hk
        ORDER BY SUM(total_tokens) DESC
    """)
    if not rows:
        return "\n".join(L + ["_no usage recorded this month_", ""])

    L += ["| harness | conf | in | out | cache | reason | total | cost | records |",
          "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    g = [0, 0, 0, 0, 0, 0.0, 0]
    for h, conf, i, o, c, r, t, cost, n in rows:
        L.append(f"| {h} | {conf} | {human(i)} | {human(o)} | {human(c)} | "
                 f"{human(r)} | {human(t)} | {usd(cost)} | {n} |")
        for idx, v in enumerate((i, o, c, r, t, cost or 0, n)):
            g[idx] += v
    L.append(f"| **TOTAL** |  | **{human(g[0])}** | **{human(g[1])}** | **{human(g[2])}** | "
             f"**{human(g[3])}** | **{human(g[4])}** | **{usd(g[5])}** | **{g[6]}** |")

    L += ["", "_by model (subtotal under harness):_", ""]
    mrows = q(con, f"""
        SELECT {HKEY} hk, COALESCE(NULLIF(model,''),'(unspecified)') model,
               SUM(total_tokens) tot, COUNT(*) n, MAX(confidence) conf
        FROM usage {WHERE_MTD}
        GROUP BY hk, model
        ORDER BY hk, tot DESC
    """)
    cur_h = None
    for h, m, tot, n, conf in mrows:
        if h != cur_h:
            L.append(f"- **{h}**")
            cur_h = h
        L.append(f"  - {m} -- {human(tot)} ({n} recs, {conf})")
    L.append("")
    return "\n".join(L)


def repo_of(cwd):
    """Resolve a working dir to a repo label (segment under .../workspace)."""
    if not cwd:
        return "(no cwd)"
    parts = [p for p in cwd.split("/") if p]
    if "workspace" in parts:
        i = parts.index("workspace")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else "(no cwd)"


def accounts(con):
    """Usage grouped by account/subscription (month-to-date).

    Grouped by the friendly `org` label (e.g. Personal / Employer / Client),
    with the login email and plan tier shown for audit. Only account-aware
    harnesses (currently claude-code) appear; legacy token-only harnesses are
    omitted to keep the table about *which account/plan* paid for the tokens.
    """
    rows = q(con, f"""
        SELECT COALESCE(NULLIF(org,''),'(unattributed)') org,
               COALESCE(NULLIF(account,''),'-') account,
               COALESCE(NULLIF(subscription,''),'-') sub,
               SUM(total_tokens) tot, SUM(cost_usd) cost, COUNT(*) n
        FROM usage {WHERE_MTD}
          AND (org IS NOT NULL OR account IS NOT NULL
               OR subscription IS NOT NULL OR harness='claude-code')
        GROUP BY org, account, sub
        ORDER BY tot DESC
    """)
    if not rows:
        return ""  # no account-aware harness recorded usage this month
    L = ["## Accounts -- usage by account / subscription (month-to-date)", "",
         "| account (org) | login | plan | total | cost | records |",
         "|---|---|---|--:|--:|--:|"]
    for org, acct, sub, tot, cost, n in rows:
        L.append(f"| {org} | {acct} | {sub} | {human(tot)} | {usd(cost)} | {n} |")
    L.append("")
    return "\n".join(L)


def repo_accounts(con):
    """Repo x account matrix: which account(s) touched each repo (month-to-date).

    The point is IP isolation -- a repo lit up under more than one account is
    flagged (warn). No policy is enforced; this is an eyeball aid.
    """
    rows = q(con, f"""
        SELECT cwd, COALESCE(NULLIF(org,''),'(unattributed)') org, SUM(total_tokens) tot
        FROM usage {WHERE_MTD} AND harness='claude-code'
        GROUP BY cwd, org
    """)
    if not rows:
        return ""
    # pivot: repo -> {org: tokens}
    orgs, by_repo = [], {}
    for cwd, org, tot in rows:
        repo = repo_of(cwd)
        if org not in orgs:
            orgs.append(org)
        by_repo.setdefault(repo, {})
        by_repo[repo][org] = by_repo[repo].get(org, 0) + (tot or 0)
    orgs.sort(key=lambda o: (o == "(unattributed)", o))  # unattributed last

    L = ["## Repos x accounts -- IP isolation check (month-to-date)", "",
         "| repo | " + " | ".join(orgs) + " | accounts | |",
         "|---|" + "--:|" * len(orgs) + "--:|---|"]
    # order repos by total desc
    for repo in sorted(by_repo, key=lambda r: -sum(by_repo[r].values())):
        cells = by_repo[repo]
        # count of *attributed* accounts that touched this repo
        n_acct = sum(1 for o in cells if o != "(unattributed)")
        flag = "⚠ multi-account" if n_acct > 1 else ""
        row = [repo] + [human(cells.get(o, 0)) if cells.get(o) else "·" for o in orgs]
        L.append("| " + " | ".join(row) + f" | {n_acct} | {flag} |")
    L.append("")
    L.append("_⚠ = repo used under more than one account this month; verify it matches "
             "your IP-isolation intent._")
    L.append("")
    return "\n".join(L)


def daily(con):
    L = ["## Daily -- harness totals + top model", "",
         "| day | harness | total | top model |", "|---|---|--:|---|"]
    # per day+harness total and that harness's leading model in one pass
    rows = q(con, f"""
        WITH agg AS (
          SELECT date(ts,'localtime') day, {HKEY} harness,
                 COALESCE(NULLIF(model,''),'(unspecified)') model,
                 SUM(total_tokens) tot
          FROM usage {WHERE_MTD}
          GROUP BY day, harness, model
        )
        SELECT day, harness, SUM(tot) htot,
               (SELECT model FROM agg a2
                 WHERE a2.day=a.day AND a2.harness=a.harness
                 ORDER BY tot DESC LIMIT 1) top_model,
               (SELECT MAX(tot) FROM agg a3
                 WHERE a3.day=a.day AND a3.harness=a.harness) top_tot
        FROM agg a
        GROUP BY day, harness
        ORDER BY day DESC, htot DESC
    """)
    if not rows:
        return "\n".join(L[:2] + ["_no usage this month_", ""])

    last_day = None
    for day, h, htot, top_model, top_tot in rows:
        day_cell = day if day != last_day else ""
        last_day = day
        L.append(f"| {day_cell} | {h} | {human(htot)} | {top_model} ({human(top_tot)}) |")
    L.append("")
    return "\n".join(L)


def hotspots(con):
    L = ["## Hotspots -- when the work happens (month-to-date)", ""]

    # day-of-week
    drows = q(con, f"""
        SELECT CAST(strftime('%w',ts,'localtime') AS INT) dow,
               SUM(total_tokens) tot, COUNT(*) n
        FROM usage {WHERE_MTD} GROUP BY dow
    """)
    by_dow = {d: (0, 0) for d in range(7)}
    for dow, tot, n in drows:
        by_dow[dow] = (tot, n)
    peak = max((v[0] for v in by_dow.values()), default=0)

    L += ["**By day-of-week**", "", "| day | total | records | | |", "|---|--:|--:|---|---|"]
    for d in [1, 2, 3, 4, 5, 6, 0]:
        tot, n = by_dow[d]
        wk = "weekend" if d in (0, 6) else ""
        L.append(f"| {DOW[(d-1) % 7]} | {human(tot)} | {n} | `{bar(tot, peak)}` | {wk} |")
    wd = sum(v[0] for d, v in by_dow.items() if d not in (0, 6))
    we = sum(v[0] for d, v in by_dow.items() if d in (0, 6))
    L += ["", f"_weekday {human(wd)} · weekend {human(we)}_", ""]

    # day-part
    prows = q(con, f"""
        SELECT CAST(strftime('%H',ts,'localtime') AS INT) hr,
               SUM(total_tokens) tot, COUNT(*) n
        FROM usage {WHERE_MTD} GROUP BY hr
    """)
    by_part = {p[0]: [0, 0] for p in DAYPARTS}
    for hr, tot, n in prows:
        for name, lo, hi in DAYPARTS:
            if lo <= hr < hi:
                by_part[name][0] += tot
                by_part[name][1] += n
                break
    ppeak = max((v[0] for v in by_part.values()), default=0)

    L += ["**By day-part**", "", "| part | total | records | |", "|---|--:|--:|---|"]
    for name, lo, hi in DAYPARTS:
        tot, n = by_part[name]
        L.append(f"| {name} ({lo:02d}-{hi:02d}) | {human(tot)} | {n} | `{bar(tot, ppeak)}` |")
    L.append("")
    return "\n".join(L)


def main():
    if not os.path.exists(DB_PATH):
        sys.exit(f"no ledger at {DB_PATH}")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    doc = "\n".join([
        f"# tokometer morning report -- {dt.datetime.now():%Y-%m-%d %H:%M}",
        "",
        advisory(con),
        claude_limits(con),
        headline(con),
        daily(con),
        hotspots(con),
        accounts(con),
        repo_accounts(con),
    ])
    con.close()

    print(doc)
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"morning-{dt.date.today()}.md")
    with open(path, "w") as f:
        f.write(doc + "\n")
    print(f"\n> written to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
