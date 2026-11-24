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
then stamp each harvested assistant message by its `sessionId`.

Claude Desktop and the VS Code extension never write prompts to history.jsonl.
Their sessions are placed by the per-session state Claude Code keeps in the
config dir it ran under, <root>/file-history/<sessionId>/ and then
<root>/session-env/<sessionId>/, when exactly one root holds it (a symlinked,
shared marker dir is ignored). On two machines on 2026-11-24 this agreed with
history.jsonl for every session that had both. Sessions with neither are left
unattributed (org NULL).

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
type=="assistant" lines. Claude Code writes each one's turns to
<session>/subagents/agent-<agentId>.jsonl, nested agents included. Those lines
have the same shape and carry the parent's sessionId, so they are read like the
session file and attributed to the session's account. A fork's file opens with
a copy of the parent's last response under the same message.id; the uid
dedupes it.

When a subagent finishes in the foreground, the parent gets a type=="user" line
with a toolUseResult block (status=="completed", agentId, resolvedModel,
usage). That usage is the agent's last response only, not its total. It is
harvested (keyed by agentId, source="agent-aggregate") only when the agent's
own transcript is absent, as with Claude Code versions that did not write one.
An agent whose aggregate the ledger already holds is not read from its file,
which would count that last response twice. Background launches
(status=="async_launched") never get a completed block; their file is their
only record.

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


def account_record(root):
    """The oauthAccount record for one config root, or {}.

    <root>/.claude.json first: with CLAUDE_CONFIG_DIR set, Claude Code keeps
    the account record there, and any <home>/.claude.json beside the root is
    left over from before. Without it (the default ~/.claude), the record is
    ~/.claude.json.
    """
    for cand in (os.path.join(root, ".claude.json"),
                 os.path.join(os.path.dirname(root), ".claude.json")):
        j = _load_json(cand)
        if j.get("oauthAccount"):
            return j["oauthAccount"]
    return {}


def resolve_identity(root):
    """(account_email, subscription_tier, org_label) for one config root."""
    creds = _load_json(os.path.join(root, ".credentials.json"))
    oauth = creds.get("claudeAiOauth") or {}
    acct = account_record(root)
    subscription = (oauth.get("subscriptionType")
                    or ORG_TYPE_SUBSCRIPTION.get(acct.get("organizationType")))
    if not subscription and (creds.get("apiKey") or os.environ.get("ANTHROPIC_API_KEY")):
        subscription = "api"
    email = acct.get("emailAddress") or acct.get("accountUuid")
    return email, subscription, friendly_org(acct.get("organizationName"))


# Per-session state Claude Code keeps in the active config dir, strongest
# evidence first: edit checkpoints, then the session's env snapshot dir.
SESSION_MARKER_DIRS = ("file-history", "session-env")


def session_markers(roots):
    """sessionId -> root, for sessions whose state dirs sit in exactly one root.

    Claude Desktop and the VS Code extension never write prompts to
    history.jsonl, but every session leaves <root>/file-history/<sessionId>/
    or <root>/session-env/<sessionId>/ in the config dir it ran under. A
    marker dir that is a symlink (shared with another root) is not evidence.
    """
    seen = {d: {} for d in SESSION_MARKER_DIRS}
    for root in roots:
        for d in SESSION_MARKER_DIRS:
            p = os.path.join(root, d)
            if os.path.islink(p) or not os.path.isdir(p):
                continue
            try:
                names = os.listdir(p)
            except OSError:
                continue
            for sid in names:
                seen[d].setdefault(sid, set()).add(root)
    out = {}
    for d in SESSION_MARKER_DIRS:
        for sid, where in seen[d].items():
            if sid not in out and len(where) == 1:
                out[sid] = next(iter(where))
    return out


def session_index(roots):
    """sessionId -> (account, subscription, org).

    The latest history.jsonl entry wins; sessions with none (Claude Desktop,
    the VS Code extension) fall back to session_markers().
    """
    idents = {root: resolve_identity(root) for root in roots}
    index = _history_index(roots, idents)
    for sid, root in session_markers(roots).items():
        index.setdefault(sid, idents[root])
    return index


def _history_index(roots, idents):
    """sessionId -> identity from history.jsonl, latest entry wins."""
    best = {}  # sid -> (timestamp, identity)
    for root in roots:
        ident = idents[root]
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


def transcripts(projects_dir):
    """Session transcripts under one projects dir, and their agents' transcripts.

    <project>/<session>.jsonl is a session; <project>/<session>/subagents/
    agent-<agentId>.jsonl is an agent it launched. Nothing else under a session
    dir is a transcript. Sorted, so a session is read before its agents.
    """
    return sorted(
        glob.glob(os.path.join(projects_dir, "*", "*.jsonl"))
        + glob.glob(os.path.join(projects_dir, "*", "*", "subagents", "agent-*.jsonl")))


def _agent_of(path):
    """agentId of a subagent transcript; None for a session transcript."""
    if os.path.basename(os.path.dirname(path)) != "subagents":
        return None
    return os.path.basename(path)[len("agent-"):-len(".jsonl")]


def _subagents_dir(path):
    """Where agents launched from this transcript keep theirs."""
    if _agent_of(path):
        return os.path.dirname(path)
    return os.path.join(path[:-len(".jsonl")], "subagents")


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

    con = ledger.connect()
    # Rows stored before responses were keyed on message.id carry
    # claude-code:<line-uuid> uids, so a re-read of an older transcript would
    # not collide with them. requestId is one per API response: skip any
    # response whose requestId the ledger already holds, under whatever uid.
    known = {r[0] for r in con.execute(
        "SELECT DISTINCT request_id FROM usage WHERE harness=? "
        "AND source='session-file' AND request_id IS NOT NULL", (HARNESS,))}
    # Agents counted from their toolUseResult before agent files were read.
    agg_prefix = f"{HARNESS}:agent:"
    stored_agents = {r[0][len(agg_prefix):] for r in con.execute(
        "SELECT uid FROM usage WHERE harness=? AND source='agent-aggregate'", (HARNESS,))}

    rows = []
    for d in dirs:
        for path in transcripts(d):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if seen_mtime.get(path) == mtime:
                continue  # unchanged; dedup still covers re-reads
            new_mtime[path] = mtime
            if _agent_of(path) in stored_agents:
                continue  # its aggregate is stored; the file would repeat its last response
            subagents = _subagents_dir(path)
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
                        if os.path.isfile(os.path.join(subagents, f"agent-{agent_id}.jsonl")):
                            continue  # read from the agent's transcript instead
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
