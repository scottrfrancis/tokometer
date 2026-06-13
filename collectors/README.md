# Collectors

Each collector is a small, independent harvester that reads what one tool already writes
locally and normalizes it into the shared SQLite ledger (`~/.tokometer/ledger.db`). They are
run by [`harvest.sh`](../harvest.sh) (which only runs the ones listed in `TOKOMETER_HARNESSES`),
but each is also runnable on its own:

```sh
python3 collectors/claude_code.py      # honors TOKOMETER_HOME (default ~/.tokometer)
```

## The contract every collector follows

All of them go through [`lib/ledger.py`](../lib/ledger.py), which gives them a uniform shape:

- **Write via `insert_usage(con, rows)`** using the `USAGE_COLS` keys. Token collectors write the
  `usage` table; the two code-metric collectors write their own tables (see below).
- **Deterministic `uid`** per row (e.g. `claude-code:<message-uuid>`) so re-harvesting is idempotent —
  `INSERT OR IGNORE` for immutable records, `on_conflict="replace"` for live totals that grow during a
  session (Droid).
- **`source`** ∈ `session-file | cli-json | admin-api | scrape | self-tally` and
  **`confidence`** ∈ `exact | estimate`, so the report never presents a guess as a hard number.
- **High-water state** in `~/.tokometer/state/<harness>.json` (`load_state`/`save_state`) and/or per-file
  mtime, so re-runs only read what changed.
- **Retention guard** (`older_than_retention`) so a collector never re-introduces rows older than the
  pruning horizon that `monthly.py` enforces.
- **Fail soft.** A collector that finds nothing prints a note and exits 0; `harvest.sh` isolates failures
  so one bad source never blocks the rest.

`ts` is always stored as ISO-8601 UTC (`...Z`); the report buckets into local time.

## At a glance

| Collector | Harness | Provider | Source | Confidence | Key env |
|---|---|---|---|---|---|
| `opencode.py` | opencode | *(varies; per-message)* | `opencode.db` message rows | exact | `OPENCODE_DB` |
| `claude_code.py` | claude-code | anthropic | `~/.claude/**/projects/*.jsonl` transcripts | exact | `CLAUDE_CONFIG_DIR`, `CLAUDE_PROFILES_GLOB`, `CLAUDE_CONFIG_ROOTS` |
| `droid.py` | droid | factory | `~/.factory/sessions/*/*.settings.json` sidecars | exact | `FACTORY_SESSIONS_GLOB` |
| `copilot.py` | copilot | github | `~/.copilot/session-state/*/events.jsonl` | **estimate** (output-only) | `COPILOT_EVENTS_GLOB` |
| `cursor_fetch.py` | cursor | cursor | Playwright scrape of the usage dashboard | — (fetch step) | *(profile dir)* |
| `cursor_reconcile.py` | cursor | cursor | dashboard exports / self-tally | estimate (exact if export has tokens) | `CURSOR_EMAIL`, `CURSOR_ADMIN_KEY` |
| `cursor_repos.py` | cursor | cursor | `~/.cursor/ai-tracking/ai-code-tracking.db` | heuristic (repo weights) | — |
| `git_metrics.py` | *(code metrics)* | — | `git log` over your repos | exact | `TOKOMETER_GIT_ROOT`, `TOKOMETER_GIT_AUTHOR`, `TOKOMETER_GIT_DEPTH` |
| `gh_metrics.py` | *(PR metrics)* | — | `gh search prs` | exact | *(uses `gh` auth)* |

---

## Token collectors

### `claude_code.py` — Claude Code (multi-account)
Reads each session transcript (`type=="assistant"` lines carry `message.usage`), one ledger row per
message, deduped on the message `uuid`. The hard part is **attribution**: Claude Code keeps all transcripts
in one shared store regardless of which account is active, so the collector reconstructs *which account ran
each session* from every profile's own `history.jsonl` (which lists the `sessionId`s prompted under that
profile). It scans `~/.claude` plus `~/.claude-profiles/*/.claude` (override via `CLAUDE_PROFILES_GLOB` /
`CLAUDE_CONFIG_ROOTS`) and stamps each row with `account` (email), `subscription` (max/team/…), and a
friendly `org`. Cost is left 0 (flat-rate seats aren't metered per token). See [DESIGN.md §3.2](../DESIGN.md).

### `opencode.py` — OpenCode
Reads the `message` table of `opencode.db` (auto-located, or `OPENCODE_DB`). Each assistant message carries
exact token counts and (sometimes) cost. Per-message, exact. High-water mark on `time_created`.

### `droid.py` — Droid (Factory)
Reads per-session `*.settings.json` sidecars under `~/.factory/sessions/`. Session totals **grow during
use**, so rows are **upserted** by session id (`on_conflict="replace"`). Uses `tokenUsage` (this session
only) to avoid double-counting child/subagent sessions.

### `copilot.py` — GitHub Copilot CLI
Parses `events.jsonl` per session. This CLI version emits **output tokens only** (no input/cache locally),
so every row is marked `estimate`. The authoritative input/cache numbers would require the org metrics API.

---

## Cursor — the holdout (a 3-step pipeline)

Cursor is the awkward one: the `cursor-agent` CLI keeps **no local token data**, and on an SSO-managed seat
there's no admin key. The only truth source is the authenticated usage dashboard. So Cursor is handled by
three cooperating scripts (run in order by `harvest.sh` when `cursor` is enabled), and Playwright is an
**optional** dependency — skip it with `TOKOMETER_SKIP_PLAYWRIGHT=1` if you don't use Cursor.

### 1. `cursor_fetch.py` — the Playwright screen-scraper *(last resort)*
Drives a real browser to pull the usage CSV off `cursor.com/dashboard/usage`:

- **We never automate your IdP/SSO.** Instead it reuses a **persistent browser profile** you log into once
  by hand. First-time setup opens a visible window:
  ```sh
  python3 collectors/cursor_fetch.py --login    # log in via SSO/MFA, then press Enter to persist
  ```
- Normal runs are **headless**: open the month-to-date usage page → click **Export CSV** → save the download
  into `~/.tokometer/cursor-exports/` → write success/failure + timestamp to
  `~/.tokometer/state/cursor_fetch.json`.
- **Exit code is non-zero on failure**, so `harvest.sh` keeps going and the morning report surfaces an
  advisory ("Cursor auto-fetch failed — re-login") instead of silently going stale.

This is the most fragile collector by far — it depends on the dashboard's DOM and your login staying valid.
Design choices worth knowing if you touch it: reuse a **persistent browser profile** (so SSO/MFA is never
automated), prefer **element waits over fixed sleeps**, and always write the **status artifact**
(`state/cursor_fetch.json`) so a failure becomes a report advisory rather than silent stale data. See the
docstring at the top of `cursor_fetch.py` for the step-by-step.

### 2. `cursor_reconcile.py` — turn dashboard data into ledger rows
Ingests, in increasing order of fidelity:
1. **self-tally** — one `estimate` row (0 tokens) per Cursor launch, so activity shows up even with no token data;
2. **exports** — usage JSON you (or `cursor_fetch`) saved under `cursor-exports/*.json`
   (handles the `/teams/filtered-usage-events` shape with `tokenUsage`); writes **exact** rows that supersede
   the self-tally estimates for the window they cover;
3. **manual override** — `cursor-exports/manual-*.json` like `{"month":"2026-06","spend_usd":12.34}` for when
   you only have the headline dollar figure;
4. **admin API** — a `TODO` stub for if a `CURSOR_ADMIN_KEY` ever becomes available.

No-Playwright capture recipe: open the dashboard, DevTools → Network, save the usage XHR response as
`~/.tokometer/cursor-exports/usage-YYYY-MM.json`.

### 3. `cursor_repos.py` — repo attribution
The usage data has no repo/path column, so it can't be tied to a repo directly. Cursor's **local AI-code
tracking DB** (`~/.cursor/ai-tracking/ai-code-tracking.db`) does record the file path + timestamp of every
AI-authored edit. This collector reads it (read-only) and records, per local-time hour, how many AI-code
records each repo got — a **weight** the report uses to split Cursor's hourly output tokens across repos.
Heuristic (matched by hour), so it's marked as such, not presented as an exact join.

---

## Code-outcome collectors (feed the Sankey / productivity panel)

These don't write `usage`; they populate `commit_metric` / `pr_metric` so the report can join *tokens spent*
to *code produced*.

### `git_metrics.py` — per-commit LOC
Walks `TOKOMETER_GIT_ROOT` **recursively** (`TOKOMETER_GIT_DEPTH` levels, vendor/worktree dirs pruned),
runs `git log` for commits authored by `TOKOMETER_GIT_AUTHOR` (comma-separated identities, or `*` for
everyone) since the start of the month, and classifies each commit's LOC into **code / docs / tests**. Repos
are labeled the same way the report's `repo_of()` labels them, so the metrics join the usage side.

### `gh_metrics.py` — merged PRs
Uses the authenticated `gh` CLI (`gh search prs`) for PRs you authored that merged this month → `pr_metric`.
Additions/deletions aren't returned by search, so they stay 0 (PR **count** is what the report surfaces).

---

## Adding a collector

1. Read whatever your tool writes locally (a DB, JSONL, sidecar, etc.).
2. Build `usage` rows keyed by `lib.ledger.USAGE_COLS`, with a deterministic `uid`, honest `source` /
   `confidence`, and `ts` normalized to UTC (`ledger.normalize_iso`).
3. Skip rows past the retention horizon (`ledger.older_than_retention`).
4. Persist a high-water mark (`ledger.load_state` / `save_state`) so re-runs are cheap.
5. `ledger.insert_usage(con, rows)` and print a one-line summary to stderr.
6. Add the script name to `ALL_HARNESSES` in [`harvest.sh`](../harvest.sh).
