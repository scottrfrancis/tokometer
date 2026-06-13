#!/usr/bin/env python3
"""Fancy HTML morning report.

Sections, top to bottom:
  1. Claude limits         -- rolling 5h/daily/weekly (+Opus weekly) usage per account.
  2. Headline stats        -- totals + per-harness table. Account-aware harnesses
                              (claude-code) split per account ("· Employer" etc).
  3. Graphs (inline SVG)   -- tokens by harness, daily totals, hour-of-day
                              (day-part), and top models.
  4. Productivity (Sankey) -- output tokens harness -> model -> repo -> code/docs/tests.
  5. Accounts              -- usage by account / subscription (friendly org label),
                              plus a repo x account matrix flagging multi-account repos.

Self-contained: inline CSS + inline SVG, no CDN/JS, works offline. Writes
$TOKOMETER_HOME/reports/morning-YYYY-MM-DD.html and prints the path.
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

COLORS = {"droid": "#8b7cff", "cursor": "#22b8cf", "copilot": "#ff922b",
          "opencode": "#20c997", "factory": "#8b7cff", "github": "#ff922b",
          "local": "#20c997", "claude-code": "#5c7cfa"}
GREY = "#8a90a6"
# stable fallback palette for composite labels (e.g. "claude-code · Employer")
PALETTE = ["#5c7cfa", "#22b8cf", "#20c997", "#ff922b", "#f06595", "#cc5de8",
           "#fcc419", "#94d82d", "#ff6b6b", "#4dabf7"]

# A "display harness": account-aware harnesses (claude-code) are split per org so
# each account pair reads as its own first-level harness across the whole report.
HKEY = ("(CASE WHEN org IS NOT NULL AND TRIM(COALESCE(org,'')) <> '' "
        "THEN harness || ' · ' || org ELSE harness END)")


def enabled_harnesses():
    """Set of harness names to surface, from TOKOMETER_HARNESSES; None = all enabled."""
    v = os.environ.get("TOKOMETER_HARNESSES")
    return set(v.split()) if v and v.strip() else None
# Reporting period. When PERIOD_START is None the report covers the current
# month-to-date; set_period('YYYY-MM') scopes it to a specific completed month.
PERIOD_START = None   # 'YYYY-MM-DD' inclusive
PERIOD_END = None     # 'YYYY-MM-DD' exclusive
IS_MONTH = False
PERIOD_LABEL = "month-to-date"


def set_period(month=None):
    """month='YYYY-MM' scopes to that calendar month; None = current MTD."""
    global PERIOD_START, PERIOD_END, IS_MONTH, PERIOD_LABEL
    if not month:
        PERIOD_START = PERIOD_END = None
        IS_MONTH = False
        PERIOD_LABEL = "month-to-date"
        return
    y, m = (int(x) for x in month.split("-"))
    PERIOD_START = f"{y:04d}-{m:02d}-01"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    PERIOD_END = f"{ny:04d}-{nm:02d}-01"
    IS_MONTH = True
    PERIOD_LABEL = dt.date(y, m, 1).strftime("%B %Y")


def pred(col="ts"):
    """SQL predicate (no WHERE) restricting `col` to the active period."""
    if PERIOD_START is None:
        return f"date({col},'localtime') >= date('now','localtime','start of month')"
    return (f"date({col},'localtime') >= '{PERIOD_START}' "
            f"AND date({col},'localtime') < '{PERIOD_END}'")


def where(col="ts"):
    return "WHERE " + pred(col)


def color(name):
    if name in COLORS:
        return COLORS[name]
    if name and " · " in name:   # composite "harness · org" -> stable distinct color
        h = 0
        for ch in name:
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        return PALETTE[h % len(PALETTE)]
    return GREY


def human(n):
    n = n or 0
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n/div:.1f}{unit}"
    return str(int(n))


def q(con, sql, p=()):
    return con.execute(sql, p).fetchall()


# ------------------------------------------------------------------------ advisory
LOGIN_CMD = "python3 ~/.tokometer/collectors/cursor_fetch.py --login"


def claude_limits(con):
    """Rolling-window Claude usage per account (5-hour / daily / weekly + Opus weekly).

    Anthropic does not publish fixed token thresholds for these windows and Claude
    Code doesn't persist live limit consumption locally, so this shows YOUR usage in
    each window (messages + tokens) computed from the ledger -- a burn-rate gauge, not
    a literal % of limit. Set TOKOMETER_CLAUDE_LIMIT_{5H,24H,7D,OPUS7D} (token counts)
    to render a % bar against your own observed thresholds.
    """
    en = enabled_harnesses()
    if en is not None and "claude_code" not in en:
        return ""
    now = dt.datetime.now(dt.timezone.utc)

    def cutoff(**kw):
        return (now - dt.timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")

    WINDOWS = [("5-hour", cutoff(hours=5), "5H", False),
               ("daily (24h)", cutoff(days=1), "24H", False),
               ("weekly (7d)", cutoff(days=7), "7D", False),
               ("Opus weekly", cutoff(days=7), "OPUS7D", True)]
    # accounts present in claude-code usage (current month or recent)
    accts = [r[0] for r in q(con, """
        SELECT COALESCE(NULLIF(org,''),'(unattributed)') org
        FROM usage WHERE harness='claude-code'
        GROUP BY org ORDER BY SUM(total_tokens) DESC""")]
    if not accts:
        return ""

    def cell(acct, since, opus):
        opus_f = "AND model LIKE '%opus%'" if opus else ""
        r = q(con, f"""SELECT COUNT(*), COALESCE(SUM(total_tokens),0)
                       FROM usage WHERE harness='claude-code'
                         AND COALESCE(NULLIF(org,''),'(unattributed)')=? AND ts>=? {opus_f}""",
              (acct, since))[0]
        return r[0], r[1]

    head = "".join(f"<th>{w[0]}</th>" for w in WINDOWS)
    trs = []
    for acct in accts:
        tds = []
        for _, since, key, opus in WINDOWS:
            msgs, tok = cell(acct, since, opus)
            thr = os.environ.get(f"TOKOMETER_CLAUDE_LIMIT_{key}")
            extra = ""
            if thr:
                try:
                    pct = tok / float(thr)
                    dgr = "bar-red" if pct >= 1 else ("bar-amber" if pct >= 0.85 else "bar-green")
                    extra = (f'<div class="track" style="margin-top:3px"><div class="fill {dgr}" '
                             f'style="width:{min(100, pct*100):.0f}%"></div></div>'
                             f'<span class="cap-pct">{pct:.0%}</span>')
                except ValueError:
                    pass
            celltxt = "·" if not msgs else f"{msgs} msg · {human(tok)}"
            tds.append(f'<td class="r">{celltxt}{extra}</td>')
        trs.append(f"<tr><td>{acct}</td>{''.join(tds)}</tr>")
    note = ("Your usage per rolling window (not a published limit — Anthropic's thresholds "
            "are dynamic). Set <code>TOKOMETER_CLAUDE_LIMIT_5H/24H/7D/OPUS7D</code> to gauge "
            "against your own observed caps.")
    return f"""
      <h2 style="font-size:15px;margin:18px 0 8px">Claude limits &middot; rolling windows
        <span class="sub" style="font-weight:400">(per account; messages &middot; tokens)</span></h2>
      <table class="stats">
        <thead><tr><th>account (org)</th>{head}</tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>
      <p class="sub" style="margin:6px 2px 0">{note}</p>"""


def advisory_html(con):
    en = enabled_harnesses()
    if en is not None and "cursor" not in en:
        return ""   # cursor disabled on this machine; no Cursor advisory
    try:
        with open(CURSOR_FETCH_STATE) as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {"ok": False, "error": "auto-fetch has never run"}
    if st.get("ok"):
        return ""

    asof = q(con, f"SELECT MAX(date(ts,'localtime')) FROM usage WHERE harness='cursor' "
                  f"AND {pred('ts')}")[0][0]
    asof_txt = (f"Showing last-good Cursor data through <b>{asof}</b> (usage only grows within a "
                f"month, so the most recent successful pull stays valid)."
                if asof else "No Cursor data yet this month.")
    return (f'<div class="advisory">'
            f'<strong>Cursor auto-fetch failed</strong> &mdash; {st.get("error","?")[:120]}<br>'
            f'{asof_txt} To refresh, log in again (one-time, opens a browser):'
            f'<pre class="cmd">{LOGIN_CMD}</pre></div>')


# ------------------------------------------------------------------- headline stats
def headline(con):
    rows = q(con, f"""
        SELECT {HKEY} hk, MAX(confidence) conf,
               SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
               SUM(total_tokens), SUM(cost_usd), SUM(credits), COUNT(*)
        FROM usage {where()} GROUP BY hk ORDER BY SUM(total_tokens) DESC""")
    tot_tok = sum(r[5] for r in rows)
    tot_cost = sum(r[6] or 0 for r in rows)
    tot_rec = sum(r[8] for r in rows)

    cards = f"""
      <div class="cards">
        <div class="card"><div class="num">{human(tot_tok)}</div><div class="lbl">total tokens (MTD)</div></div>
        <div class="card"><div class="num">${tot_cost:,.2f}</div><div class="lbl">spend (MTD)</div></div>
        <div class="card"><div class="num">{tot_rec}</div><div class="lbl">records</div></div>
      </div>"""

    trs = []
    for h, conf, i, o, c, t, cost, cr, n in rows:
        dot = f'<span class="dot" style="background:{color(h)}"></span>'
        badge = "ok" if conf == "exact" else "est"
        trs.append(f"""<tr>
          <td>{dot}{h}</td><td class="b-{badge}">{conf}</td>
          <td class="r">{human(i)}</td><td class="r">{human(o)}</td>
          <td class="r">{human(c)}</td><td class="r">{human(t)}</td>
          <td class="r">${(cost or 0):,.2f}</td><td class="r">{human(cr)}</td><td class="r">{n}</td></tr>""")
    table = f"""
      <table class="stats">
        <thead><tr><th>harness</th><th>conf</th><th>in</th><th>out</th><th>cache</th>
          <th>total</th><th>cost</th><th>credits</th><th>recs</th></tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>"""
    return cards + table


def accounts(con):
    """Usage by account/subscription, grouped by friendly org label."""
    rows = q(con, f"""
        SELECT COALESCE(NULLIF(org,''),'(unattributed)') org,
               COALESCE(NULLIF(account,''),'-') account,
               COALESCE(NULLIF(subscription,''),'-') sub,
               SUM(total_tokens), SUM(cost_usd), COUNT(*)
        FROM usage {where()}
          AND (org IS NOT NULL OR account IS NOT NULL
               OR subscription IS NOT NULL OR harness='claude-code')
        GROUP BY org, account, sub ORDER BY 4 DESC""")
    if not rows:
        return ""
    trs = []
    for org, acct, sub, t, cost, n in rows:
        trs.append(f"""<tr>
          <td>{org}</td><td>{acct}</td><td>{sub}</td>
          <td class="r">{human(t)}</td><td class="r">${(cost or 0):,.2f}</td>
          <td class="r">{n}</td></tr>""")
    return f"""
      <h2 style="font-size:15px;margin:18px 0 8px">Accounts</h2>
      <table class="stats">
        <thead><tr><th>account (org)</th><th>login</th><th>plan</th>
          <th>total</th><th>cost</th><th>recs</th></tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>{repo_accounts(con)}"""


def repo_accounts(con):
    """Repo x account matrix; flags repos lit up under more than one account."""
    rows = q(con, f"""
        SELECT cwd, COALESCE(NULLIF(org,''),'(unattributed)') org, SUM(total_tokens)
        FROM usage {where()} AND harness='claude-code'
        GROUP BY cwd, org""")
    if not rows:
        return ""
    orgs, by_repo = [], {}
    for cwd, org, tot in rows:
        repo = repo_of(cwd) or "(no cwd)"
        if org not in orgs:
            orgs.append(org)
        cell = by_repo.setdefault(repo, {})
        cell[org] = cell.get(org, 0) + (tot or 0)
    orgs.sort(key=lambda o: (o == "(unattributed)", o))
    head = "".join(f"<th>{o}</th>" for o in orgs)
    trs = []
    for repo in sorted(by_repo, key=lambda r: -sum(by_repo[r].values())):
        cells = by_repo[repo]
        n_acct = sum(1 for o in cells if o != "(unattributed)")
        flag = '<span class="b-est">multi</span>' if n_acct > 1 else ""
        tds = "".join(
            f'<td class="r">{human(cells[o]) if cells.get(o) else "·"}</td>' for o in orgs)
        trs.append(f"<tr><td>{repo}</td>{tds}<td class='r'>{flag}</td></tr>")
    return f"""
      <h2 style="font-size:15px;margin:18px 0 8px">Repos &times; accounts
        <span class="sub" style="font-weight:400">(IP-isolation check; "multi" = touched by &gt;1 account)</span></h2>
      <table class="stats">
        <thead><tr><th>repo</th>{head}<th></th></tr></thead>
        <tbody>{''.join(trs)}</tbody>
      </table>"""


# ----------------------------------------------------------------------- SVG charts
def svg_hbar(title, data, note=""):
    """data: list of (label, value, color). Returns inner chart content."""
    if not data:
        return f'<h3>{title}</h3><p class="empty">no data</p>'
    peak = max(v for _, v, _ in data) or 1
    row_h, lab_w, bar_w, pad = 30, 150, 360, 8
    h = pad * 2 + row_h * len(data)
    W = lab_w + bar_w + 90
    parts = [f'<svg viewBox="0 0 {W} {h}" width="100%" preserveAspectRatio="xMinYMin meet">']
    for idx, (label, val, col) in enumerate(data):
        y = pad + idx * row_h
        bw = max(2, bar_w * val / peak)
        parts.append(f'<text x="{lab_w-8}" y="{y+row_h/2+4}" text-anchor="end" class="svg-lab">{label}</text>')
        parts.append(f'<rect x="{lab_w}" y="{y+5}" width="{bw:.1f}" height="{row_h-12}" rx="3" fill="{col}"/>')
        parts.append(f'<text x="{lab_w+bw+6}" y="{y+row_h/2+4}" class="svg-val">{human(val)}</text>')
    parts.append("</svg>")
    n = f'<p class="note">{note}</p>' if note else ""
    return f'<h3>{title}</h3>{"".join(parts)}{n}'


def svg_vbar(title, data, note="", highlight_peak=True):
    """data: list of (label, value, color?). Returns inner chart content."""
    if not data:
        return f'<h3>{title}</h3><p class="empty">no data</p>'
    vals = [v for _, v, *_ in data]
    peak = max(vals) or 1
    n = len(data)
    plot_h, top, bot, gap = 150, 14, 38, 6
    bw = max(8, min(48, int(640 / n) - gap))
    W = n * (bw + gap) + 30
    H = plot_h + top + bot
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMinYMin meet">']
    for idx, item in enumerate(data):
        label, val = item[0], item[1]
        col = item[2] if len(item) > 2 else "#5b8def"
        if highlight_peak and val == peak:
            col = "#ffd43b"
        x = 20 + idx * (bw + gap)
        bh = max(1, plot_h * val / peak)
        y = top + (plot_h - bh)
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bw}" height="{bh:.1f}" rx="2" fill="{col}"/>')
        parts.append(f'<text x="{x+bw/2}" y="{H-bot+14}" text-anchor="middle" class="svg-ax">{label}</text>')
        if val and (val == peak or n <= 16):
            parts.append(f'<text x="{x+bw/2}" y="{y-3:.1f}" text-anchor="middle" class="svg-tip">{human(val)}</text>')
    parts.append("</svg>")
    nt = f'<p class="note">{note}</p>' if note else ""
    return f'<h3>{title}</h3>{"".join(parts)}{nt}'


def chart_by_harness(con):
    rows = q(con, f"SELECT {HKEY} hk, SUM(total_tokens) FROM usage {where()} "
                  "GROUP BY hk ORDER BY 2 DESC")
    data = [(h, v, color(h)) for h, v in rows]
    return svg_hbar("Tokens by harness (MTD)", data)


def chart_daily(con):
    rows = q(con, f"SELECT date(ts,'localtime') d, SUM(total_tokens) FROM usage {where()} "
                  "GROUP BY d ORDER BY d")
    data = [(d[5:], v, "#5b8def") for d, v in rows]   # MM-DD label
    return svg_vbar("Daily total tokens (MTD)", data, note="peak day highlighted")


def chart_hourly(con):
    rows = q(con, f"SELECT CAST(strftime('%H',ts,'localtime') AS INT) hr, SUM(total_tokens) "
                  f"FROM usage {where()} GROUP BY hr")
    by_hr = {h: 0 for h in range(24)}
    for hr, v in rows:
        by_hr[hr] = v
    data = [(f"{h:02d}", by_hr[h], "#9775fa") for h in range(24)]
    return svg_vbar("Usage by hour of day (day-part, local time)", data,
                    note="Droid attributes a whole session to its last-activity hour; "
                         "Cursor/OpenCode/Copilot are per-event. Peak hour highlighted.")


def chart_models(con):
    rows = q(con, f"""SELECT {HKEY} hk, COALESCE(NULLIF(model,''),'(unspecified)') m,
                      SUM(total_tokens) t FROM usage {where()}
                      GROUP BY hk, m ORDER BY t DESC LIMIT 8""")
    data = [(f"{m[:26]}", t, color(h)) for h, m, t in rows]
    return svg_hbar("Top models by tokens (MTD)", data)


# ----------------------------------------------------- productivity Sankey
ATTR_WINDOW_S = 30 * 60   # 30-minute time+cwd attribution window
CAT_COLOR = {"code": "#37b24d", "docs": "#4dabf7", "tests": "#f59f00",
             "overhead": "#5c6470", "unattributed": "#3a3f4b"}


def repo_of(cwd):
    """Resolve a working dir to a repo label (basename under ~/workspace)."""
    if not cwd:
        return None
    parts = [p for p in cwd.split("/") if p]
    if "workspace" in parts:
        i = parts.index("workspace")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else None


def short_model(m):
    if not m:
        return "(unspecified)"
    return m.split("/")[-1][:22]


def productivity_flows(con):
    """Return (links, node_labels, totals) for the output-token Sankey + summary.

    links: dict[(layer,src,dst,harness)] = output_tokens, conserved across 4 layers
    Layers: 0 harness, 1 model, 2 repo, 3 output-category.
    """
    # repo -> LOC by class (MTD) decides the repo->category split
    loc = {}
    for repo, code, docs, test in q(con, f"""
            SELECT repo, SUM(code_add+code_del), SUM(docs_add+docs_del),
                   SUM(test_add+test_del)
            FROM commit_metric
            WHERE {pred('ts')}
            GROUP BY repo"""):
        loc[repo] = {"code": code or 0, "docs": docs or 0, "tests": test or 0}

    # Cursor's usage CSV is repo-blind; infer its repo from the local AI-code
    # tracking DB. cw[hour][repo] = activity hits; cwd_day is the same-day fallback
    # for Cursor hours that have token usage but no recorded AI-code activity.
    cw, cw_day = {}, {}
    for hour, repo, hits in q(con, """SELECT hour, repo, hits FROM cursor_repo_hour"""):
        cw.setdefault(hour, {})[repo] = hits
        day = cw_day.setdefault(hour[:10], {})
        day[repo] = day.get(repo, 0) + hits

    rows = q(con, f"""SELECT {HKEY} hk, model, cwd, SUM(output_tokens)
                      FROM usage {where()} AND harness != 'cursor'
                      GROUP BY hk, model, cwd""")
    cur_rows = q(con, f"""SELECT strftime('%Y-%m-%dT%H', ts, 'localtime') hr,
                                 model, SUM(output_tokens)
                          FROM usage WHERE harness = 'cursor' AND {pred('ts')}
                          GROUP BY hr, model""")
    links = {}
    cat_tokens = {"code": 0, "docs": 0, "tests": 0, "overhead": 0, "unattributed": 0}

    def add(layer, src, dst, h, v):
        links[(layer, src, dst, h)] = links.get((layer, src, dst, h), 0) + v

    def route_category(m, repo, h, val):
        """Layer 2->3: split a model->repo flow into code/docs/tests by LOC mix."""
        add(1, m, repo, h, val)
        mix = loc.get(repo)
        tot = (mix["code"] + mix["docs"] + mix["tests"]) if mix else 0
        if not tot:
            add(2, repo, "overhead", h, val)
            cat_tokens["overhead"] += val
            return
        for cat in ("code", "docs", "tests"):
            share = val * mix[cat] / tot
            if share > 0:
                add(2, repo, cat, h, share)
                cat_tokens[cat] += share

    for h, model, cwd, out in rows:
        if not out:
            continue
        m = short_model(model)
        repo = repo_of(cwd)
        add(0, h, m, h, out)
        if repo is None:
            add(1, m, "(no repo)", h, out)
            add(2, "(no repo)", "unattributed", h, out)
            cat_tokens["unattributed"] += out
            continue
        route_category(m, repo, h, out)

    # Cursor: split each hour's output tokens across repos by that hour's AI-code
    # activity share (same-day fallback when the exact hour has no activity).
    for hr, model, out in cur_rows:
        if not out:
            continue
        m = short_model(model)
        add(0, "cursor", m, "cursor", out)
        dist = cw.get(hr) or cw_day.get(hr[:10])
        if not dist:
            add(1, m, "(no repo)", "cursor", out)
            add(2, "(no repo)", "unattributed", "cursor", out)
            cat_tokens["unattributed"] += out
            continue
        wtot = sum(dist.values())
        for repo, w in dist.items():
            route_category(m, repo, "cursor", out * w / wtot)
    return links, cat_tokens, loc


def sankey_svg(links):
    """Layered SVG Sankey; widths proportional to output tokens (conserved)."""
    if not links:
        return '<p class="empty">no attributable output-token flow yet</p>'
    LAYERS = 4
    # node value per layer: outgoing for layer 0, incoming for layers 1..3
    # (flow is conserved, so this avoids double-counting middle nodes)
    nodes = [{} for _ in range(LAYERS)]
    for (layer, src, dst, h), v in links.items():
        if layer == 0:
            nodes[0][src] = nodes[0].get(src, 0) + v
        nodes[layer + 1][dst] = nodes[layer + 1].get(dst, 0) + v

    W, H = 920, 460
    pad_top, pad_bot = 24, 14
    node_w = 14
    col_gap = (W - node_w) / (LAYERS - 1)
    usable = H - pad_top - pad_bot
    total = sum(nodes[0].values()) or 1

    # order + y-position nodes per layer
    pos = {}            # (layer,name) -> (y_top, height)
    for layer in range(LAYERS):
        items = sorted(nodes[layer].items(), key=lambda kv: -kv[1])
        gap = 6
        scale = (usable - gap * (len(items) - 1)) / total
        y = pad_top
        for name, val in items:
            h = max(2, val * scale)
            pos[(layer, name)] = [y, h, y, y]   # y_top, height, out_cursor, in_cursor
            y += h + gap

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMinYMin meet">']
    # ribbons first (under nodes)
    scale_v = usable / total  # same vertical scale per token across layers? per-layer differs; use link thickness from source scale
    for (layer, src, dst, h), v in sorted(links.items(), key=lambda kv: -kv[1]):
        s = pos[(layer, src)]
        d = pos[(layer + 1, dst)]
        s_scale = s[1] / nodes[layer][src]
        d_scale = d[1] / nodes[layer + 1][dst]
        th_s = v * s_scale
        th_d = v * d_scale
        x0 = node_w + layer * col_gap
        x1 = layer * col_gap + col_gap
        y0 = s[2]; s[2] += th_s
        y1 = d[3]; d[3] += th_d
        cx0, cx1 = x0 + (x1 - x0) * 0.5, x0 + (x1 - x0) * 0.5
        path = (f"M{x0},{y0:.1f} C{cx0:.1f},{y0:.1f} {cx1:.1f},{y1:.1f} {x1},{y1:.1f} "
                f"L{x1},{y1 + th_d:.1f} C{cx1:.1f},{y1 + th_d:.1f} {cx0:.1f},{y0 + th_s:.1f} {x0},{y0 + th_s:.1f} Z")
        parts.append(f'<path d="{path}" fill="{color(h)}" fill-opacity="0.32"/>')

    # nodes + labels
    for layer in range(LAYERS):
        x = layer * col_gap
        for name, val in nodes[layer].items():
            y, hgt = pos[(layer, name)][0], pos[(layer, name)][1]
            fill = CAT_COLOR.get(name, "#aab2c5") if layer == 3 else "#8089a0"
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{node_w}" height="{hgt:.1f}" rx="2" fill="{fill}"/>')
            label = f"{name} ({human(val)})"
            if layer == LAYERS - 1:
                tx, anchor = x - 6, "end"
            else:
                tx, anchor = x + node_w + 5, "start"
            # always label the endpoints (harness/output); intermediate only if tall
            if hgt >= 9 or layer in (0, LAYERS - 1):
                parts.append(f'<text x="{tx:.1f}" y="{y + hgt/2 + 3:.1f}" text-anchor="{anchor}" class="sk-lab">{label}</text>')
    # column headers
    heads = ["harness", "model", "repo", "output"]
    for i, hd in enumerate(heads):
        x = i * col_gap + (node_w if i < 3 else -node_w)
        anchor = "end" if i == 3 else "start"
        parts.append(f'<text x="{x:.1f}" y="12" text-anchor="{anchor}" class="sk-head">{hd}</text>')
    parts.append("</svg>")
    return "".join(parts)


def attributed_outputs(con):
    """Credit each commit (30-min cwd window) to the harness with the most
    output tokens in that repo just before the commit. Returns summary rows."""
    commits = q(con, f"""SELECT repo, ts, code_add+test_add+docs_add AS added,
                        code_add, docs_add, test_add
                        FROM commit_metric
                        WHERE {pred('ts')}""")
    usage = q(con, f"""SELECT harness, cwd, ts, output_tokens
                       FROM usage {where()} AND cwd IS NOT NULL""")
    # index usage by repo
    by_repo = {}
    for h, cwd, ts, out in usage:
        r = repo_of(cwd)
        if r:
            by_repo.setdefault(r, []).append((ts, h, out or 0))
    tally = {}
    for repo, cts, added, ca, da, ta in commits:
        lo = (dt.datetime.fromisoformat(cts.replace("Z", "+00:00"))
              - dt.timedelta(seconds=ATTR_WINDOW_S)).isoformat().replace("+00:00", "Z")
        best_h, best_v = "unattributed", 0
        for uts, h, out in by_repo.get(repo, []):
            if lo <= uts <= cts and out > best_v:
                best_h, best_v = h, out
        t = tally.setdefault(best_h, {"commits": 0, "loc": 0})
        t["commits"] += 1
        t["loc"] += added or 0
    return tally


def build_productivity(con):
    links, cat, loc = productivity_flows(con)
    svg = sankey_svg(links)

    pr_total = q(con, f"""SELECT COUNT(*), COALESCE(SUM(additions),0) FROM pr_metric
                         WHERE {pred('merged_at')}""")[0]
    loc_tot = {k: sum(v[k] for v in loc.values()) for k in ("code", "docs", "tests")}
    cards = f"""
      <div class="cards">
        <div class="card"><div class="num">{loc_tot['code']:,}</div><div class="lbl">code LOC touched (MTD)</div></div>
        <div class="card"><div class="num">{loc_tot['docs']:,}</div><div class="lbl">docs LOC</div></div>
        <div class="card"><div class="num">{loc_tot['tests']:,}</div><div class="lbl">test LOC</div></div>
        <div class="card"><div class="num">{pr_total[0]}</div><div class="lbl">PRs merged</div></div>
      </div>"""

    unattr = cat.get("unattributed", 0)
    attr = sum(v for k, v in cat.items() if k != "unattributed")
    callout = ""
    if unattr:
        pct = unattr / (unattr + attr) * 100 if (unattr + attr) else 0
        callout = (f'<div class="advisory" style="background:#1a2230;border-color:#2a3a52;color:#9ec5fe">'
                   f'<strong>{human(unattr)} output tokens ({pct:.0f}%) remain unattributed</strong> — '
                   f'dirless sessions, or Cursor hours with no recorded local AI-code activity to infer a repo from. '
                   f'The remaining {human(attr)} is attributed to a repo.</div>')

    tally = attributed_outputs(con)
    trs = []
    for h, t in sorted(tally.items(), key=lambda kv: -kv[1]["loc"]):
        dot = f'<span class="dot" style="background:{color(h)}"></span>'
        trs.append(f'<tr><td>{dot}{h}</td><td class="r">{t["commits"]}</td><td class="r">{t["loc"]:,}</td></tr>')
    attr_table = f"""
      <table class="stats" style="max-width:420px">
        <thead><tr><th>credited harness (30-min window)</th><th>commits</th><th>LOC added</th></tr></thead>
        <tbody>{''.join(trs) or '<tr><td colspan=3 class="empty">no commits in window</td></tr>'}</tbody>
      </table>"""

    note = ('<p class="note">Flow width = <b>output tokens</b> (work done, not cache), conserved '
            'harness&rarr;model&rarr;repo&rarr;output. Output category split by LOC mix from your commits; '
            'repos with tokens but no commits = <i>overhead</i>. '
            'Cursor&rsquo;s usage export is repo-blind, so its repo is <i>inferred</i> from Cursor&rsquo;s local '
            'AI-code tracking by matching the hour (same-day fallback) &mdash; an estimate, not an exact token map. '
            'Terminal labels and the table below show real git/PR numbers. '
            'Attribution is time+cwd (fuzzy causal), not a literal token&rarr;line conversion.</p>')
    return f"""
    <h2 style="font-size:15px;margin:22px 0 8px">Productivity &middot; tokens in, code out (MTD)</h2>
    {cards}
    {callout}
    <div class="chart full">{svg}</div>
    {note}
    {attr_table}"""


# ------------------------------------------------------------------------- assemble
CSS = """
:root{--bg:#0f1117;--panel:#171a23;--line:#262b38;--fg:#e6e8ef;--mut:#8a90a6;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:28px;}
h1{font-size:20px;margin:0 0 4px} .sub{color:var(--mut);margin:0 0 20px}
.cap-pct{text-align:right;font-variant-numeric:tabular-nums}
.track{background:#0e1018;border-radius:6px;height:12px;width:180px;overflow:hidden}
.fill{height:100%}.bar-green{background:#37b24d}.bar-amber{background:#f59f00}.bar-red{background:#e03131}
.cards{display:flex;gap:14px;margin:6px 0 16px}
.card{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .num{font-size:26px;font-weight:700}.card .lbl{color:var(--mut);font-size:12px;margin-top:2px}
table.stats{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:22px}
table.stats th{color:var(--mut);font-weight:500;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
table.stats td{padding:6px 8px;border-bottom:1px solid var(--line)}
table.stats td.r{text-align:right;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
.b-ok{color:#51cf66}.b-est{color:#ffa94d}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.chart h3{margin:0 0 10px;font-size:14px}
.chart.full{grid-column:1 / -1}
.svg-lab{fill:var(--fg);font-size:12px}.svg-val{fill:var(--mut);font-size:11px}
.svg-ax{fill:var(--mut);font-size:9px}.svg-tip{fill:var(--fg);font-size:9px}
.note{color:var(--mut);font-size:11px;margin:8px 0 0}.empty{color:var(--mut)}
.advisory{background:#2a1f10;border:1px solid #5c4708;border-radius:10px;padding:12px 16px;margin-bottom:16px;color:#ffd8a8}
.advisory ul{margin:6px 0 0;padding-left:18px}.advisory code{background:#0e1018;padding:1px 5px;border-radius:4px}
.advisory pre.cmd{background:#0e1018;border:1px solid #5c4708;border-radius:6px;padding:10px 12px;margin:10px 0 0;color:#ffe8b3;font-family:ui-monospace,Menlo,monospace;font-size:13px;overflow-x:auto;user-select:all}
.sk-lab{fill:var(--fg);font-size:10px}.sk-head{fill:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.08em}
"""


def build(con):
    now = dt.datetime.now()
    if IS_MONTH:
        # historical month report: the live cursor-fetch advisory is about *now*,
        # so it doesn't belong here.
        title = f"tokometer monthly report &middot; {PERIOD_LABEL}"
        sub = f"{PERIOD_LABEL} &middot; full month &middot; generated {now:%Y-%m-%d}"
        top = ""
    else:
        title = "tokometer morning report"
        sub = f"{now:%A, %B %d %Y &middot; %H:%M} &middot; month-to-date"
        top = advisory_html(con)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><style>{CSS}</style></head><body>
<h1>{title}</h1>
<p class="sub">{sub}</p>
{top}
{claude_limits(con)}
<h2 style="font-size:15px;margin:18px 0 8px">Headline</h2>
{headline(con)}
<h2 style="font-size:15px;margin:4px 0 10px">Graphs</h2>
<div class="grid">
  <div class="chart">{chart_by_harness(con)}</div>
  <div class="chart">{chart_models(con)}</div>
  <div class="chart full">{chart_daily(con)}</div>
  <div class="chart full">{chart_hourly(con)}</div>
</div>
{build_productivity(con)}
{accounts(con)}
</body></html>"""


def generate(month=None):
    """Render the report for `month` ('YYYY-MM') or current MTD, return its path."""
    if not os.path.exists(DB_PATH):
        sys.exit(f"no ledger at {DB_PATH}")
    set_period(month)
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    html = build(con)
    con.close()
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = f"month-{month}.html" if month else f"morning-{dt.date.today()}.html"
    path = os.path.join(REPORT_DIR, name)
    with open(path, "w") as f:
        f.write(html)
    return path


def main():
    month = None
    if "--month" in sys.argv:
        i = sys.argv.index("--month")
        month = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    print(generate(month))


if __name__ == "__main__":
    main()
