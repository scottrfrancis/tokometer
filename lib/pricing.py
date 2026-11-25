"""Anthropic API list prices, for an approximate "value" of Claude usage.

Subscription plans charge a flat fee, so `cost_usd` is zero for most Claude
rows. Value answers a different question: what the same tokens would have
cost on the API at list price. It is an order-of-magnitude figure for judging
a plan, a harness or a model, not a bill. Only Claude models are priced;
self-hosted and other vendors' models return None.

Prices are first-party API rates in US dollars per million tokens, as of
2026-11. Bedrock and Vertex set their own prices; those ids are priced at the
first-party rate too.
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

_KEYS = sorted(PRICES, key=len, reverse=True)
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


def value_usd(model, input_tokens, output_tokens, cache_read, cache_write, reasoning):
    """List-price value of one usage row in dollars, or None for a non-Claude model.

    Reasoning tokens are billed as output. Collectors that already count
    reasoning inside output_tokens store 0 here, so nothing is counted twice.
    """
    key = price_key(model)
    if key is None:
        return None
    i, o, cr, cw, r = (n or 0 for n in (input_tokens, output_tokens, cache_read,
                                        cache_write, reasoning))
    p_in, p_out, p_read = PRICES[key]
    long_prompt = LONG_PROMPT.get(key)
    if long_prompt and i + cr + cw > long_prompt[0]:
        p_in, p_out, p_read = long_prompt[1]
    return (i * p_in + (o + r) * p_out + cr * p_read + cw * p_in * CACHE_WRITE_X) / 1e6


# SQL fragment summing value over the usage rows of a query; needs register(con).
SUM_VALUE = ("SUM(list_value(model, input_tokens, output_tokens, cache_read_tokens,"
             " cache_write_tokens, reasoning_tokens))")


def register(con):
    """Make list_value(model, input, output, cache_read, cache_write, reasoning)
    available in SQL on `con`. SUM over it is NULL when no row is Claude."""
    con.create_function("list_value", 6, value_usd, deterministic=True)
