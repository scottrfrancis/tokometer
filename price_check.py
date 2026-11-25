#!/usr/bin/env python3
"""Monthly recheck of lib/pricing.py against OpenRouter.

daily.sh runs this every day. It does nothing until 30 days have passed
since the last good check; a failed check is retried on the next run. When
it runs it compares:

- each Claude price in PRICES with OpenRouter's catalog, which lists Claude
  at Anthropic's list price;
- each OPEN_WEIGHT price with the median across OpenRouter's providers for
  that model, the same rule the table was built with;

and lists Claude models OpenRouter offers that PRICES lacks. A price that
moved more than TOLERANCE is drift. The result goes to
state/price_check.json, and the morning report shows any drift, new models
or a failing check. Nothing is changed automatically: updating the table is a
person's call.

Usage: python3 price_check.py [--force]
"""
import datetime as dt
import json
import os
import statistics
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import pricing  # noqa: E402

TOKOMETER_HOME = os.path.expanduser(os.environ.get("TOKOMETER_HOME", "~/.tokometer"))
STATE = os.path.join(TOKOMETER_HOME, "state", "price_check.json")
CATALOG_URL = "https://openrouter.ai/api/v1/models"
MAX_AGE_DAYS = 30
TOLERANCE = 0.20
FIELDS = ("input", "output", "cache_read")


def endpoints_url(model_id):
    return f"https://openrouter.ai/api/v1/models/{model_id}/endpoints"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tokometer-price-check"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _rates(p):
    """($ input, $ output, $ cache read) per million tokens from an OpenRouter
    pricing dict; no cache price means cache reads bill as input."""
    per_m = lambda k: float(p.get(k) or 0) * 1e6
    i = per_m("prompt")
    return i, per_m("completion"), per_m("input_cache_read") or i


def median_rates(endpoints):
    rates = [_rates(e["pricing"]) for e in endpoints]
    return tuple(statistics.median(r[n] for r in rates) for n in range(3))


def compare(ours, theirs):
    """Fields whose price moved more than TOLERANCE, and ids OpenRouter lacks."""
    drift, unlisted = [], []
    for mid, mine in sorted(ours.items()):
        if mid not in theirs:
            unlisted.append(mid)
            continue
        for field, a, b in zip(FIELDS, mine, theirs[mid]):
            if abs(b - a) > TOLERANCE * max(a, 1e-9):
                drift.append({"id": mid, "field": field, "ours": a, "theirs": round(b, 4)})
    return drift, unlisted


def check(fetch):
    catalog = {m["id"]: m for m in fetch(CATALOG_URL)["data"]}
    ours = {pricing.openrouter_id(k): v for k, v in pricing.PRICES.items()}
    theirs = {mid: _rates(catalog[mid]["pricing"]) for mid in ours if mid in catalog}
    for mid, v in pricing.OPEN_WEIGHT.items():
        ours[mid] = v
        eps = fetch(endpoints_url(mid))["data"]["endpoints"]
        if eps:
            theirs[mid] = median_rates(eps)
    drift, unlisted = compare(ours, theirs)
    new_claude = sorted(mid for mid in catalog
                        if mid.startswith("anthropic/claude-") and ":" not in mid
                        and pricing.exact_key(mid) is None)
    return {"drift": drift, "new_claude": new_claude, "unlisted": unlisted}


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def run(path=STATE, fetch=fetch_json, now=None, force=False):
    """Check unless the last good check is under MAX_AGE_DAYS old; return the state."""
    now = now or dt.datetime.now(dt.timezone.utc)
    path = str(path)
    prev = _load(path)
    last_good = prev.get("checked_at")
    if (not force and prev.get("ok") and last_good
            and (now.date() - dt.date.fromisoformat(last_good)).days < MAX_AGE_DAYS):
        return prev
    try:
        state = {"ok": True, "checked_at": now.date().isoformat(), **check(fetch)}
    except Exception as e:  # network, HTTP or a changed response shape
        state = {**prev, "ok": False, "checked_at": last_good,
                 "error": f"{type(e).__name__}: {e}"[:300]}
        state.setdefault("drift", [])
        state.setdefault("new_claude", [])
    state["last_attempt"] = now.isoformat(timespec="seconds")
    _save(path, state)
    return state


def main():
    state = run(force="--force" in sys.argv)
    if not state["ok"]:
        print(f"price check failed: {state['error']}")
        return 1
    print(f"price check {state['checked_at']}: {len(state['drift'])} drifted, "
          f"{len(state['new_claude'])} new Claude models")
    for d in state["drift"]:
        print(f"  {d['id']} {d['field']}: ${d['ours']} -> ${d['theirs']}")
    for mid in state["new_claude"]:
        print(f"  new: {mid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
