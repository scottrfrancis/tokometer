#!/usr/bin/env python3
"""Claude Code harvester (multi-account via profiles).

This machine runs Claude Code under several accounts using a HOME-swapping
"profile" scheme. The catch: only the *config* is per-profile -- the transcript
store is SHARED. Each profile's `projects/` is a symlink to ~/.claude/projects,
so a transcript line records the repo (`cwd`) but NOT which account was active.

Authoritative attribution comes from each profile's own (un-symlinked) history:
  <root>/history.jsonl   -- one line per prompt: {sessionId, project(cwd), timestamp}
Because Claude Code writes history.jsonl under the *active* profile's HOME, the
sessionIds it lists were unambiguously run under that profile's account. We union
every profile's history into a sessionId -> (account, subscription, org) index,
then stamp each harvested assistant message by its `sessionId`. Sessions with no
history entry (older, or never prompted) are left unattributed (org NULL).

Config roots scanned for identity + history:
  ~/.claude                          -- default login
  ~/.claude-profiles/<name>/.claude  -- one per profile (al, bs, ss, ...)
Identity per root:
  subscription <- <root>/.credentials.json claudeAiOauth.subscriptionType (max|team|...)
  account      <- <root>/../.claude.json    oauthAccount.emailAddress
  org          <- oauthAccount.organizationName, personal orgs mapped to 'Personal'

Transcripts are read ONCE from the (de-duplicated, realpath'd) shared projects
dir. type=="assistant" lines carry message.usage {input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens}; one ledger row per message
(dedup on message uuid). Thinking folds into output_tokens (reasoning_tokens=0);
`<synthetic>`/empty turns are dropped by the zero-token guard. cost_usd stays 0
(flat-rate seats; not metered).

Each run also reconciles: any already-stored row whose session has since become
attributable is back-filled, so attribution is correct even if a prompt's history
entry lands after the message was first harvested.

Overrides (mainly for testing):
  CLAUDE_CONFIG_DIR     default config root (else ~/.claude)
  CLAUDE_PROFILES_GLOB  glob for profile roots (else ~/.claude-profiles/*/.claude)
  CLAUDE_CONFIG_ROOTS   os.pathsep-separated explicit root list; replaces discovery
"""
import os
import sys
import json
import glob

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402

HARNESS = "claude-code"
PROVIDER = "anthropic"


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def config_roots():
    """Claude config roots (default login + every profile), order preserved."""
    if os.environ.get("CLAUDE_CONFIG_ROOTS"):
        raw = os.environ["CLAUDE_CONFIG_ROOTS"].split(os.pathsep)
    else:
        raw = [os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")]
        raw += sorted(glob.glob(os.path.expanduser(
            os.environ.get("CLAUDE_PROFILES_GLOB", "~/.claude-profiles/*/.claude"))))
    seen, roots = set(), []
    for r in raw:
        p = os.path.realpath(os.path.expanduser(r))
        if os.path.isdir(p) and p not in seen:
            seen.add(p)
            roots.append(p)
    return roots


def friendly_org(org_name):
    """Readable grouping label; personal/no-org collapse to 'Personal'."""
    if org_name and not org_name.endswith("'s Organization"):
        return org_name
    return "Personal"


def resolve_identity(root):
    """(account_email, subscription_tier, org_label) for one config root."""
    creds = _load_json(os.path.join(root, ".credentials.json"))
    oauth = creds.get("claudeAiOauth") or {}
    subscription = oauth.get("subscriptionType")
    if not subscription and (creds.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY")):
        subscription = "api"
    acct = {}
    for cand in (os.path.join(os.path.dirname(root), ".claude.json"),
                 os.path.join(root, ".claude.json")):
        j = _load_json(cand)
        if j.get("oauthAccount"):
            acct = j["oauthAccount"]
            break
    email = acct.get("emailAddress") or acct.get("accountUuid")
    return email, subscription, friendly_org(acct.get("organizationName"))


def session_index(roots):
    """sessionId -> (account, subscription, org), latest history entry wins."""
    best = {}  # sid -> (timestamp, identity)
    for root in roots:
        ident = resolve_identity(root)
        hp = os.path.join(root, "history.jsonl")
        try:
            fh = open(hp, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = o.get("sessionId")
                if not sid:
                    continue
                try:
                    ts = int(o.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0
                if sid not in best or ts >= best[sid][0]:
                    best[sid] = (ts, ident)
    return {sid: ident for sid, (_, ident) in best.items()}


def transcript_dirs(roots):
    """De-duplicated realpath'd projects dirs (the shared store collapses to one)."""
    seen, dirs = set(), []
    for root in roots:
        p = os.path.realpath(os.path.join(root, "projects"))
        if os.path.isdir(p) and p not in seen:
            seen.add(p)
            dirs.append(p)
    return dirs


def harvest():
    roots = config_roots()
    if not roots:
        print(f"[{HARNESS}] no Claude Code config roots; skipping", file=sys.stderr)
        return 0

    sess = session_index(roots)
    dirs = transcript_dirs(roots)

    state = ledger.load_state(HARNESS)
    seen_mtime = state.get("file_mtime", {})
    new_mtime = dict(seen_mtime)

    rows = []
    read = 0
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*", "*.jsonl"))):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if seen_mtime.get(path) == mtime:
                continue  # unchanged; dedup still covers re-reads
            new_mtime[path] = mtime
            try:
                fh = open(path, errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"assistant"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if o.get("type") != "assistant":
                        continue
                    u = (o.get("message") or {}).get("usage") or {}
                    inp = int(u.get("input_tokens") or 0)
                    out = int(u.get("output_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    if inp + out + cr + cw == 0:
                        continue  # synthetic / empty turns
                    ts = ledger.normalize_iso(o.get("timestamp"))
                    if ledger.older_than_retention(ts):
                        continue
                    sid = o.get("sessionId")
                    account, subscription, org = sess.get(sid, (None, None, None))
                    uid = o.get("uuid") or f"{sid}:{o.get('requestId')}"
                    read += 1
                    rows.append({
                        "uid": f"{HARNESS}:{uid}",
                        "ts": ts,
                        "harness": HARNESS,
                        "provider": PROVIDER,
                        "model": (o.get("message") or {}).get("model"),
                        "session_id": sid,
                        "request_id": o.get("requestId"),
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read_tokens": cr,
                        "cache_write_tokens": cw,
                        "reasoning_tokens": 0,
                        "credits": 0,
                        "cost_usd": 0.0,
                        "source": "session-file",
                        "confidence": "exact",
                        "raw_ref": f"{path}#{uid}",
                        "cwd": o.get("cwd"),
                        "account": account,
                        "subscription": subscription,
                        "org": org,
                    })

    con = ledger.connect()
    inserted = ledger.insert_usage(con, rows)
    reattr = reconcile(con, sess)
    con.close()

    ledger.save_state(HARNESS, {"file_mtime": new_mtime})
    matched = sum(1 for r in rows if r["org"] is not None)
    print(f"[{HARNESS}] {len(roots)} roots, {len(sess)} mapped sessions; "
          f"{read} read ({matched} attributed), {inserted} new, {reattr} back-filled",
          file=sys.stderr)
    return inserted


def reconcile(con, sess):
    """Back-fill org/account/subscription for rows whose session became attributable
    after they were first harvested (history can lag the transcript). Idempotent."""
    changed = 0
    cur = con.execute(
        "SELECT DISTINCT session_id FROM usage "
        "WHERE harness=? AND org IS NULL AND session_id IS NOT NULL", (HARNESS,))
    stale = [r[0] for r in cur.fetchall()]
    before = con.total_changes
    for sid in stale:
        ident = sess.get(sid)
        if not ident or ident[2] is None:
            continue
        account, subscription, org = ident
        con.execute(
            "UPDATE usage SET account=?, subscription=?, org=? "
            "WHERE harness=? AND session_id=? AND org IS NULL",
            (account, subscription, org, HARNESS, sid))
    con.commit()
    changed = con.total_changes - before
    return changed


if __name__ == "__main__":
    harvest()
