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
  $CLAUDE_CONFIG_DIR                 -- if exported in the harvesting shell
  ~/.claude-profiles/<name>/.claude  -- one per profile (al, bs, ss, ...)
  ~/.claude-<name>                   -- config dirs a shell hook selects through
                                        CLAUDE_CONFIG_DIR (e.g. per client repo);
                                        only dirs holding history.jsonl or
                                        .claude.json
Identity per root:
  account      <- <root>/.claude.json oauthAccount.emailAddress, else
                  <root>/../.claude.json (the default ~/.claude)
  subscription <- <root>/.credentials.json claudeAiOauth.subscriptionType, else
                  oauthAccount.organizationType (claude_max -> max, ...): on
                  macOS the token is in the Keychain and the file is absent
  org          <- oauthAccount.organizationName, personal orgs mapped to 'Personal'

Transcripts are read ONCE from the (de-duplicated, realpath'd) shared projects
dir. type=="assistant" lines carry message.usage {input_tokens, output_tokens,
cache_read_input_tokens, cache_creation_input_tokens}; one ledger row per API
response. Claude Code writes one line per content block of a response (thinking,
text, tool_use). Those lines share message.id, requestId and an identical usage
block, but each has its own uuid, so rows are keyed on message.id (then
requestId, then the line uuid) and a file's lines for one response collapse to
the last one seen. Thinking folds into output_tokens (reasoning_tokens=0);
`<synthetic>`/empty turns are dropped by the zero-token guard. cost_usd stays 0
(flat-rate seats; not metered).

Some assistant lines carry a nested message.usage.iterations array -- multiple
billed API attempts folded under one transcript line (e.g. an overload retry:
a "message" attempt followed by a "fallback_message" attempt). The top-level
usage fields mirror only the LAST iteration, so when iterations is present we
sum input/output/cache tokens across all of them instead of trusting the
top-level fields.

Subagents (the "Agent" tool) do not appear in the session transcript's
type=="assistant" lines. Newer Claude Code versions write each subagent's turns
to <session>/subagents/agent-<agentId>.jsonl; the transcript glob in harvest()
is deliberately non-recursive and does not read those files. Instead, when a
subagent finishes, a type=="user" line carries a top-level toolUseResult block
with a usage summary (status=="completed", plus agentId/resolvedModel/usage).
We harvest one row per such block, keyed by agentId, with
source="agent-aggregate". Reading the subagent files as well would count a
completed subagent twice. Async launches (status=="async_launched", no usage)
are skipped, and they never get a completed block, so their usage is not
harvested at all: a known undercount.

Each run also reconciles: any already-stored row whose session has since become
attributable is back-filled, so attribution is correct even if a prompt's history
entry lands after the message was first harvested.

Overrides (mainly for testing):
  CLAUDE_CONFIG_DIR     an additional config root
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


def _usage_tokens(u):
    """(input, output, cache_read, cache_write) from a message.usage dict.

    When usage.iterations is present (multiple billed API attempts folded
    under one transcript line, e.g. an overload fallback retry), the
    top-level fields only mirror the LAST iteration -- sum across all
    iterations instead so earlier attempts' tokens aren't lost.
    """
    iterations = u.get("iterations")
    if iterations:
        inp = sum(int(it.get("input_tokens") or 0) for it in iterations)
        out = sum(int(it.get("output_tokens") or 0) for it in iterations)
        cr = sum(int(it.get("cache_read_input_tokens") or 0) for it in iterations)
        cw = sum(int(it.get("cache_creation_input_tokens") or 0) for it in iterations)
        return inp, out, cr, cw
    return (
        int(u.get("input_tokens") or 0),
        int(u.get("output_tokens") or 0),
        int(u.get("cache_read_input_tokens") or 0),
        int(u.get("cache_creation_input_tokens") or 0),
    )


def _response_key(o):
    """Dedup key for an assistant line: the API response it belongs to.

    Claude Code writes one line per content block (thinking, text, tool_use).
    The lines share message.id and requestId but each has its own uuid, so the
    uuid alone would store one response once per block.
    """
    msg = o.get("message") or {}
    return msg.get("id") or o.get("requestId") or o.get("uuid")


# organizationType in the account record -> subscription tier, for roots whose
# login token is in the macOS Keychain and so has no .credentials.json.
ORG_TYPE_SUBSCRIPTION = {
    "claude_pro": "pro",
    "claude_max": "max",
    "claude_team": "team",
    "claude_enterprise": "enterprise",
}


def _is_config_dir(path):
    return (os.path.isfile(os.path.join(path, "history.jsonl"))
            or os.path.isfile(os.path.join(path, ".claude.json")))


def config_roots():
    """Claude config roots (default login, every profile, every routed config
    dir), order preserved."""
    if os.environ.get("CLAUDE_CONFIG_ROOTS"):
        raw = os.environ["CLAUDE_CONFIG_ROOTS"].split(os.pathsep)
    else:
        # ~/.claude always, even when CLAUDE_CONFIG_DIR is exported: a shell
        # hook that routes it per project leaves it set in the shell a manual
        # harvest runs from.
        raw = ["~/.claude"]
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            raw.append(os.environ["CLAUDE_CONFIG_DIR"])
        raw += sorted(glob.glob(os.path.expanduser(
            os.environ.get("CLAUDE_PROFILES_GLOB", "~/.claude-profiles/*/.claude"))))
        raw += sorted(p for p in glob.glob(os.path.expanduser("~/.claude-*"))
                      if _is_config_dir(p))
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
    acct = {}
    # <root>/.claude.json first: with CLAUDE_CONFIG_DIR set, Claude Code keeps
    # the account record there, and any <home>/.claude.json beside the root is
    # left over from before. Without it (the default ~/.claude), the record is
    # ~/.claude.json.
    for cand in (os.path.join(root, ".claude.json"),
                 os.path.join(os.path.dirname(root), ".claude.json")):
        j = _load_json(cand)
        if j.get("oauthAccount"):
            acct = j["oauthAccount"]
            break
    subscription = (oauth.get("subscriptionType")
                    or ORG_TYPE_SUBSCRIPTION.get(acct.get("organizationType")))
    if not subscription and (creds.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY")):
        subscription = "api"
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
    for d in dirs:
        # Non-recursive on purpose: <session>/subagents/agent-*.jsonl is not
        # read, because the completed toolUseResult aggregate below already
        # covers those agents (see the module docstring).
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
            responses = {}  # response key -> row; the last line of a response wins
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line or ('"assistant"' not in line and '"toolUseResult"' not in line):
                        continue
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    otype = o.get("type")

                    if otype == "assistant":
                        u = (o.get("message") or {}).get("usage") or {}
                        inp, out, cr, cw = _usage_tokens(u)
                        if inp + out + cr + cw == 0:
                            continue  # synthetic / empty turns
                        ts = ledger.normalize_iso(o.get("timestamp"))
                        if ledger.older_than_retention(ts):
                            continue
                        key = _response_key(o)
                        if not key:
                            continue  # nothing to dedupe on
                        sid = o.get("sessionId")
                        account, subscription, org = sess.get(sid, (None, None, None))
                        responses[key] = {
                            "uid": f"{HARNESS}:{key}",
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
                            "raw_ref": f"{path}#{key}",
                            "cwd": o.get("cwd"),
                            "account": account,
                            "subscription": subscription,
                            "org": org,
                        }

                    elif otype == "user":
                        tur = o.get("toolUseResult")
                        if not isinstance(tur, dict) or tur.get("status") != "completed":
                            continue  # e.g. async_launched -- no usage yet, nothing to tie it to
                        au = tur.get("usage")
                        agent_id = tur.get("agentId")
                        if not isinstance(au, dict) or not agent_id:
                            continue
                        inp, out, cr, cw = _usage_tokens(au)
                        if inp + out + cr + cw == 0:
                            continue
                        ts = ledger.normalize_iso(o.get("timestamp"))
                        if ledger.older_than_retention(ts):
                            continue
                        sid = o.get("sessionId")
                        account, subscription, org = sess.get(sid, (None, None, None))
                        rows.append({
                            "uid": f"{HARNESS}:agent:{agent_id}",
                            "ts": ts,
                            "harness": HARNESS,
                            "provider": PROVIDER,
                            "model": tur.get("resolvedModel"),
                            "session_id": sid,
                            "request_id": None,
                            "input_tokens": inp,
                            "output_tokens": out,
                            "cache_read_tokens": cr,
                            "cache_write_tokens": cw,
                            "reasoning_tokens": 0,
                            "credits": 0,
                            "cost_usd": 0.0,
                            "source": "agent-aggregate",
                            "confidence": "exact",
                            "raw_ref": f"{path}#{agent_id}",
                            "cwd": o.get("cwd"),
                            "account": account,
                            "subscription": subscription,
                            "org": org,
                        })
            rows.extend(responses.values())

    con = ledger.connect()
    # Rows stored before responses were keyed on message.id carry
    # claude-code:<line-uuid> uids, so a re-read of an older transcript would
    # not collide with them. requestId is one per API response: skip any
    # response whose requestId the ledger already holds, under whatever uid.
    known = {r[0] for r in con.execute(
        "SELECT DISTINCT request_id FROM usage WHERE harness=? "
        "AND source='session-file' AND request_id IS NOT NULL", (HARNESS,))}
    fresh = [r for r in rows
             if r["source"] != "session-file" or r["request_id"] not in known]
    inserted = ledger.insert_usage(con, fresh)
    reattr = reconcile(con, sess)
    con.close()

    ledger.save_state(HARNESS, {"file_mtime": new_mtime})
    read = len(rows)
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
