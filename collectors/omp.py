#!/usr/bin/env python3
"""OMP harvester.

Source: session transcripts at ~/.omp/agent/sessions/<cwd-slug>/<ts>_<uuid>.jsonl
(OMP_SESSIONS_DIR overrides), one JSON record per line, opening with a
type=="session" header that carries the session id and cwd. Usage comes from
two record types:

  * type=="message" with message.role=="assistant": a turn, with
    message.{provider, model, usage} and message.timestamp (epoch ms, the
    request start);
  * type=="model_usage": a side call made outside the turn (title, recap,
    compaction, composer prediction, job captions), with top-level provider,
    model, purpose, usage and an ISO timestamp.

usage = {input, output, cacheRead, cacheWrite, totalTokens, reasoningTokens,
cost:{total}}. input excludes cacheRead (totalTokens is the sum of the four)
and output already includes reasoning, so reasoning_tokens stays 0 and the
ledger's total_tokens equals totalTokens. reasoningTokens is not a reliable
subset either: a length-capped reply can report more reasoning than output.

Rows are keyed omp:<session id>:<record id> and inserted once. A file is read
again only when its mtime changes, so a turn that finishes after a harvest is
picked up by the next one whatever its timestamp.

Who paid. OMP is provider-based, so the provider decides the "account":
  * a provider in models.yml (~/.omp/agent/models.yml; OMP_MODELS_YML
    overrides) whose baseUrl is on this machine, the LAN or the tailnet is a
    self-hosted model server: account=<provider name>,
    subscription="self-hosted", org="Local". Provider names are OMP's own, so
    the same server can appear under two names when machines reach it by
    different routes (e.g. a loopback name on its host, a LAN name elsewhere).
  * a cloud provider OMP is logged in to (its agent.db auth_credentials;
    OMP_AGENT_DB overrides): account=<login email>. For anthropic, plan and
    org come from the Claude Code account record with the same email and
    organization uuid. Only the provider and identity_key columns are read,
    never the credential itself. With several logins for one provider there
    is no telling which served a turn, so the row stays unattributed.
  * anything else: account/subscription/org NULL.
cost_usd is OMP's recorded cost (0 for self-hosted models).
"""
import glob
import ipaddress
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib import ledger  # noqa: E402
from collectors import claude_code  # noqa: E402

HARNESS = "omp"
SELF_HOSTED = ("self-hosted", "Local")
# OMP's built-in local server providers, for when models.yml does not define them.
BUILTIN_LOCAL = {"ollama", "lm-studio", "lmstudio", "llama.cpp", "vllm"}
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale addresses
PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".ts.net")


def _agent_path(env, name):
    p = os.environ.get(env)
    return os.path.expanduser(p) if p else os.path.expanduser(f"~/.omp/agent/{name}")


def sessions_dir():
    return _agent_path("OMP_SESSIONS_DIR", "sessions")


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s[1:-1]
    return s


def provider_base_urls(text):
    """provider name -> baseUrl from models.yml text.

    tokometer is stdlib-only, so this reads just the shape OMP uses: a
    top-level `providers:` map whose entries each carry a `baseUrl:` key.
    """
    urls = {}
    in_providers = False
    entry_indent = child_indent = None
    current = None
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0:
            in_providers = s == "providers:"
            entry_indent = current = None
            continue
        if not in_providers:
            continue
        if entry_indent is None:
            entry_indent = indent
        if indent == entry_indent:
            current = _unquote(s[:-1]) if s.endswith(":") else None
            child_indent = None
        elif current is not None and indent > entry_indent:
            if child_indent is None:
                child_indent = indent
            m = re.match(r"baseUrl:\s*(.+)$", s)
            if indent == child_indent and m:
                urls.setdefault(current, _unquote(m.group(1)))
    return urls


def is_self_hosted(url):
    """Whether a base URL points at this machine, the LAN or the tailnet."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or "." not in host or host.endswith(PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or (
        ip.version == 4 and ip in CGNAT)


def self_hosted_providers():
    try:
        with open(_agent_path("OMP_MODELS_YML", "models.yml"), encoding="utf-8") as f:
            urls = provider_base_urls(f.read())
    except OSError:
        urls = {}
    local = {name for name, url in urls.items() if is_self_hosted(url)}
    return local | (BUILTIN_LOCAL - set(urls))


def omp_logins():
    """provider -> identity_key for OMP's own cloud logins (None when a
    provider has several). Reads only the provider and identity columns."""
    path = os.path.abspath(_agent_path("OMP_AGENT_DB", "agent.db"))
    if not os.path.isfile(path):
        return {}
    try:
        con = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(auth_credentials)")}
        if not {"provider", "identity_key"} <= cols:
            return {}
        where = " WHERE disabled_cause IS NULL" if "disabled_cause" in cols else ""
        rows = con.execute(f"SELECT provider, identity_key FROM auth_credentials{where}").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    logins = {}
    for provider, ident in rows:
        if provider and ident:
            logins[provider] = None if provider in logins else ident
    return logins


def login_identity(provider, identity_key):
    """(account, subscription, org) for an OMP cloud login."""
    email, _, rest = identity_key.partition("|")
    org_uuid = rest[len("org:"):] if rest.startswith("org:") else None
    if provider == "anthropic":
        for root in claude_code.config_roots():
            acct = claude_code.account_record(root)
            if acct.get("emailAddress") == email and (
                    not org_uuid or acct.get("organizationUuid") == org_uuid):
                return claude_code.resolve_identity(root)
    return (email or None), None, None


def identities():
    """provider -> (account, subscription, org) for every provider we can place."""
    out = {p: (p,) + SELF_HOSTED for p in self_hosted_providers()}
    for provider, ident in omp_logins().items():
        if ident and provider not in out:
            out[provider] = login_identity(provider, ident)
    return out


def _usage_row(o, session_id, cwd, path):
    """A ledger row from a turn or side-call record, or None."""
    t = o.get("type")
    if t == "message":
        rec = o.get("message") or {}
        if rec.get("role") != "assistant":
            return None
        ts_ms = rec.get("timestamp")
        ts = (ledger.iso_utc(epoch_ms=ts_ms) if isinstance(ts_ms, (int, float))
              else ledger.normalize_iso(o.get("timestamp")))
    elif t == "model_usage":
        rec = o
        ts = ledger.normalize_iso(o.get("timestamp"))
    else:
        return None
    u = rec.get("usage") or {}
    if not any(u.get(k) for k in ("input", "output", "cacheRead", "cacheWrite")):
        return None  # zero-token stubs (e.g. connection-refused errors)
    model = rec.get("model") or ""
    if model.endswith(":latest"):
        model = model[: -len(":latest")]
    return {
        "uid": f"{HARNESS}:{session_id}:{o.get('id')}",
        "ts": ts,
        "harness": HARNESS,
        "provider": rec.get("provider"),
        "model": model,
        "session_id": session_id,
        "request_id": None,
        "input_tokens": int(u.get("input") or 0),
        "output_tokens": int(u.get("output") or 0),
        "cache_read_tokens": int(u.get("cacheRead") or 0),
        "cache_write_tokens": int(u.get("cacheWrite") or 0),
        "reasoning_tokens": 0,
        "credits": 0,
        "cost_usd": float((u.get("cost") or {}).get("total") or 0.0),
        "source": "session-file",
        "confidence": "exact",
        "raw_ref": f"{path}#{o.get('id')}",
        "cwd": cwd,
    }


def harvest():
    root = sessions_dir()
    files = sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))
    if not files:
        print(f"[{HARNESS}] no session files under {root}; skipping", file=sys.stderr)
        return 0

    who = identities()
    state = ledger.load_state(HARNESS)
    seen_mtime = state.get("file_mtime", {})
    new_mtime = dict(seen_mtime)

    rows = []
    for path in files:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if seen_mtime.get(path) == mtime:
            continue
        new_mtime[path] = mtime
        session_id = os.path.basename(path).rsplit("_", 1)[-1].removesuffix(".jsonl")
        cwd = None
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue  # includes a line still being written
                if o.get("type") == "session":
                    session_id = o.get("id") or session_id
                    cwd = o.get("cwd") or cwd
                    continue
                row = _usage_row(o, session_id, cwd, path)
                if row is None or ledger.older_than_retention(row["ts"]):
                    continue
                row["account"], row["subscription"], row["org"] = who.get(
                    row["provider"], (None, None, None))
                rows.append(row)

    con = ledger.connect()
    inserted = ledger.insert_usage(con, rows)
    con.close()

    ledger.save_state(HARNESS, {"file_mtime": new_mtime})
    local = sum(1 for r in rows if r["org"] == SELF_HOSTED[1])
    print(f"[{HARNESS}] {len(files)} session files, {len(rows)} read "
          f"({local} self-hosted), {inserted} new", file=sys.stderr)
    return inserted


if __name__ == "__main__":
    harvest()
