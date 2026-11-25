"""API prices, for an approximate "value" of usage that costs nothing per token.

Subscription plans charge a flat fee and self-hosted servers charge nothing,
so `cost_usd` is zero for most rows. Value answers a different question: what
the same tokens would have cost on an API. It is an order-of-magnitude figure
for judging a plan, a harness, a server or a model, not a bill.

Two tables, in US dollars per million tokens, as of 2026-11-25:

- PRICES: Claude at Anthropic's first-party list price. Bedrock and Vertex
  set their own prices; those ids are priced at the first-party rate too.
- OPEN_WEIGHT: open-weight models the self-hosted servers run, at the median
  of OpenRouter's providers for the same weights.

Other models return None. price_check.py compares both tables with
OpenRouter once a month and the morning report shows what moved.
"""
from __future__ import annotations

import functools
import re

# A cache write costs 1.25x input for the 5-minute TTL and 2x for the 1-hour
# TTL. The ledger stores one cache_write count, and Claude Code transcripts on
# Studio (November 2026) wrote about half of each, so writes use the midpoint.
CACHE_WRITE_X = (1.25 + 2.0) / 2

# model key -> ($ input, $ output, $ cache read) per million tokens
PRICES = {
    "fable-5-2": (8.00, 40.00, 0.20),
    "mythos-5-2": (8.00, 40.00, 0.20),
    "fable-5-1": (10.00, 50.00, 0.25),
    "mythos-5-1": (10.00, 50.00, 0.25),
    "fable-5": (10.00, 50.00, 1.00),
    "mythos-5": (10.00, 50.00, 1.00),
    "opus-5-5": (4.00, 20.00, 0.20),
    "opus-5": (5.00, 25.00, 0.50),
    "opus-4-8": (5.00, 25.00, 0.50),
    "opus-4-7": (5.00, 25.00, 0.50),
    "opus-4-6": (5.00, 25.00, 0.50),
    "opus-4-5": (5.00, 25.00, 0.50),
    "opus-4-1": (15.00, 75.00, 1.50),
    "opus-4": (15.00, 75.00, 1.50),
    "opus-3": (15.00, 75.00, 1.50),
    "sonnet-5-5": (2.00, 10.00, 0.10),
    "sonnet-5": (2.00, 10.00, 0.20),
    "sonnet-4-6": (3.00, 15.00, 0.30),
    "sonnet-4-5": (3.00, 15.00, 0.30),
    "sonnet-4": (3.00, 15.00, 0.30),
    "sonnet-3-7": (3.00, 15.00, 0.30),
    "sonnet-3-5": (3.00, 15.00, 0.30),
    "haiku-5-5": (0.10, 0.50, 0.01),
    "haiku-4-5": (1.00, 5.00, 0.10),
    "haiku-3-5": (0.80, 4.00, 0.08),
    "haiku-3": (0.25, 1.25, 0.03),
}

# Models whose rates step up when the prompt (input + cache read + cache
# write) passes a size: key -> (prompt tokens, rates above it).
LONG_PROMPT = {
    "haiku-5-5": (100_000, (0.50, 2.50, 0.05)),
}

# A Claude id with no listed version is priced as the current model of its
# family.
FAMILY_DEFAULT = {
    "fable": "fable-5-2",
    "mythos": "mythos-5-2",
    "opus": "opus-5-5",
    "sonnet": "sonnet-5-5",
    "haiku": "haiku-5-5",
}

# OpenRouter id -> ($ input, $ output, $ cache read) per million tokens: the
# median across OpenRouter's providers of that model, each field taken
# separately; a provider with no cache price counts at its input price.
OPEN_WEIGHT = {
    "qwen/qwen3.8-flash": (0.15, 0.47, 0.016),      # Qwen3.8-Flash-Next weights, 3 providers
    "qwen/qwen3.8-27b": (0.1875, 1.99, 0.05),       # 22 providers
    "qwen/qwen3.6-35b-a3b": (0.10, 0.925, 0.09),    # 10 providers
}

# Server-side names that point at a model rather than naming it. They move
# when a server's primary changes (okf-knowledge infra/ai-stack.md), so the
# monthly price check is also the time to confirm these still hold.
ALIASES = {
    ("dev-ai", "workhorse"): "qwen/qwen3.8-27b",
    ("dev-ai", "qwen27-nvfp4"): "qwen/qwen3.8-27b",
    ("studio", "coder"): "qwen/qwen3.8-flash",
}

_KEYS = sorted(PRICES, key=len, reverse=True)
_OPEN_KEYS = sorted(OPEN_WEIGHT, key=len, reverse=True)
# Older ids put the version first: claude-3-5-haiku, claude-4.5-sonnet.
_VERSION_FIRST = re.compile(r"claude-(\d+(?:-\d+)?)-(opus|sonnet|haiku)")


@functools.lru_cache(maxsize=None)
def price_key(model):
    """The PRICES key for a model id, or None when it is not a Claude model.

    Handles ids as other harnesses write them: Bedrock prefixes and suffixes
    (anthropic.claude-haiku-4-5-20251001-v1:0), dotted versions
    (claude-sonnet-4.5), router prefixes (anthropic/claude-...) and Claude
    Code's [1m] suffix.
    """
    if not model:
        return None
    s = model.lower().rsplit("/", 1)[-1].replace(".", "-")
    if "claude" not in s:
        return None
    s = _VERSION_FIRST.sub(r"claude-\2-\1", s)
    for key in _KEYS:
        # not followed by a digit, so fable-5-2 does not match fable-5-20260101
        if re.search(re.escape(key) + r"(?!\d)", s):
            return key
    for family, key in FAMILY_DEFAULT.items():
        if family in s:
            return key
    return None


def exact_key(openrouter_or_model_id):
    """The PRICES key spelled out by an id, with no family fallback.

    price_check uses it to spot Claude models OpenRouter lists that the table
    lacks: anthropic/claude-opus-6 has no key, though price_key would price it
    as the current Opus.
    """
    s = openrouter_or_model_id.lower().rsplit("/", 1)[-1].replace(".", "-")
    s = _VERSION_FIRST.sub(r"claude-\2-\1", s)
    key = s.removeprefix("claude-")
    return key if key in PRICES else None


def openrouter_id(key):
    """OpenRouter's id for a PRICES key: opus-5-5 -> anthropic/claude-opus-5.5."""
    return "anthropic/claude-" + re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", key)


@functools.lru_cache(maxsize=None)
def open_weight_key(provider, model):
    """The OPEN_WEIGHT id for a self-hosted model, or None.

    Matches the weights' name inside the served id, so quantised builds
    (Qwen3.8-Flash-Next-oQ4e-mtp) find their model, then server aliases.
    """
    if not model:
        return None
    alias = ALIASES.get((provider, model))
    if alias:
        return alias
    s = model.lower()
    for key in _OPEN_KEYS:
        if key.split("/", 1)[1] in s:
            return key
    return None


def value_usd(model, input_tokens, output_tokens, cache_read, cache_write, reasoning,
              provider=None):
    """API-price value of one usage row in dollars, or None for an unpriced model.

    Reasoning tokens are billed as output. Collectors that already count
    reasoning inside output_tokens store 0 here, so nothing is counted twice.
    Open-weight cache writes bill as input: OpenRouter lists no write premium
    for them, and the local servers report none.
    """
    i, o, cr, cw, r = (n or 0 for n in (input_tokens, output_tokens, cache_read,
                                        cache_write, reasoning))
    key = price_key(model)
    if key is not None:
        p_in, p_out, p_read = PRICES[key]
        long_prompt = LONG_PROMPT.get(key)
        if long_prompt and i + cr + cw > long_prompt[0]:
            p_in, p_out, p_read = long_prompt[1]
        p_write = p_in * CACHE_WRITE_X
    else:
        open_key = open_weight_key(provider, model)
        if open_key is None:
            return None
        p_in, p_out, p_read = OPEN_WEIGHT[open_key]
        p_write = p_in
    return (i * p_in + (o + r) * p_out + cr * p_read + cw * p_write) / 1e6


# SQL fragment summing value over the usage rows of a query; needs register(con).
SUM_VALUE = ("SUM(list_value(model, input_tokens, output_tokens, cache_read_tokens,"
             " cache_write_tokens, reasoning_tokens, provider))")


def register(con):
    """Make list_value(model, input, output, cache_read, cache_write, reasoning,
    provider) available in SQL on `con`. SUM over it is NULL when no row has a
    price."""
    con.create_function("list_value", 7, value_usd, deterministic=True)
