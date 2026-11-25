"""lib.pricing prices Claude usage at Anthropic's API list prices.

The figure is an API-equivalent value for subscription use, so it only has to
be right to an order of magnitude. Non-Claude models are not priced.
"""

from __future__ import annotations

import sqlite3

import pytest

from lib import pricing

M = 1_000_000


def test_prices_each_token_kind_at_its_list_rate():
    # Opus 5.5: $4 in, $20 out, $0.20 cache read per million tokens
    assert pricing.value_usd("claude-opus-5-5", M, M, M, 0, 0) == pytest.approx(24.20)


def test_cache_writes_are_priced_between_the_5_minute_and_1_hour_rates():
    # 5-minute writes cost 1.25x input and 1-hour writes 2x; Claude Code
    # writes about half of each, so a write is priced at 1.625x input.
    assert pricing.value_usd("claude-opus-5-5", 0, 0, 0, M, 0) == pytest.approx(6.50)


def test_reasoning_tokens_are_priced_as_output():
    assert pricing.value_usd("claude-opus-5-5", 0, 0, 0, 0, M) == pytest.approx(20.0)


def test_the_most_specific_version_wins():
    # Fable 5.1 reads cache at $0.25; Fable 5 at $1.00
    assert pricing.value_usd("claude-fable-5-1", 0, 0, M, 0, 0) == pytest.approx(0.25)
    assert pricing.value_usd("claude-fable-5", 0, 0, M, 0, 0) == pytest.approx(1.00)


@pytest.mark.parametrize("model, per_m_input", [
    ("anthropic.claude-haiku-4-5-20251001-v1:0", 1.00),   # Bedrock id
    ("us.anthropic.claude-opus-4-5-20251101-v1:0", 5.00),  # Bedrock inference profile
    ("claude-sonnet-4.5", 3.00),                           # Copilot dotted name
    ("anthropic/claude-sonnet-5-5", 2.00),                 # router-style prefix
    ("claude-opus-5-5[1m]", 4.00),                         # Claude Code 1M suffix
    ("claude-sonnet-4-20250514", 3.00),                    # dated snapshot
    ("claude-3-5-haiku-20241022", 0.80),                   # old naming order
])
def test_model_ids_from_other_harnesses_resolve(model, per_m_input):
    assert pricing.value_usd(model, M, 0, 0, 0, 0) == pytest.approx(per_m_input)


def test_an_unknown_claude_version_falls_back_to_its_family():
    assert pricing.value_usd("claude-opus-99", M, 0, 0, 0, 0) == pytest.approx(4.00)


@pytest.mark.parametrize("model", ["qwen-a", "gpt-5", "workhorse", "", None])
def test_other_models_are_not_priced(model):
    assert pricing.value_usd(model, M, M, M, M, M) is None


# Self-hosted open-weight models are priced at the median of OpenRouter's
# providers for the same weights (pricing.OPEN_WEIGHT).

@pytest.mark.parametrize("provider, model, rates", [
    ("omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", (0.15, 0.47, 0.016)),
    ("studio", "Qwen3.8-Flash-Next-oQ2.5e-fp16-mtp", (0.15, 0.47, 0.016)),
    ("omlx", "Qwen3.8-27B-oQ8e-mtp", (0.1875, 1.99, 0.05)),
    ("llama.cpp", "qwen3.6-35b-a3b-mtp", (0.10, 0.925, 0.09)),
])
def test_open_weight_models_are_priced_at_the_openrouter_median(provider, model, rates):
    assert pricing.value_usd(model, M, M, M, 0, 0, provider) == pytest.approx(sum(rates))


@pytest.mark.parametrize("provider, model, same_as", [
    ("dev-ai", "workhorse", "qwen/qwen3.8-27b"),
    ("dev-ai", "qwen27-nvfp4", "qwen/qwen3.8-27b"),
    ("studio", "coder", "qwen/qwen3.8-flash"),
])
def test_server_aliases_resolve_to_the_model_they_point_at(provider, model, same_as):
    i, o, c = pricing.OPEN_WEIGHT[same_as]
    assert pricing.value_usd(model, M, M, M, 0, 0, provider) == pytest.approx(i + o + c)


def test_an_alias_only_counts_on_its_own_server():
    assert pricing.value_usd("coder", M, 0, 0, 0, 0, "omlx") is None
    assert pricing.value_usd("workhorse", M, 0, 0, 0, 0, "studio") is None


def test_open_weight_cache_writes_bill_as_input():
    i, _, _ = pricing.OPEN_WEIGHT["qwen/qwen3.8-27b"]
    assert pricing.value_usd("workhorse", 0, 0, 0, M, 0, "dev-ai") == pytest.approx(i)


def test_haiku_5_5_prices_long_prompts_at_the_higher_tier():
    # $0.10 / $0.50 up to 100K prompt tokens, $0.50 / $2.50 above
    assert pricing.value_usd("claude-haiku-5-5", 50_000, 0, 0, 0, 0) == pytest.approx(0.005)
    assert pricing.value_usd("claude-haiku-5-5", 60_000, 0, 60_000, 0, 0) == pytest.approx(
        60_000 * 0.50 / M + 60_000 * 0.05 / M)


def test_null_token_counts_count_as_zero():
    assert pricing.value_usd("claude-opus-5-5", None, M, None, None, None) == pytest.approx(20.0)


def test_register_adds_a_sql_function():
    con = sqlite3.connect(":memory:")
    pricing.register(con)
    rows = con.execute(
        "SELECT SUM(list_value(m, 1000000, 0, 0, 0, 0, p)) FROM"
        " (SELECT 'claude-opus-5-5' m, 'anthropic' p UNION ALL SELECT 'qwen', 'omlx'"
        "  UNION ALL SELECT 'claude-sonnet-5-5', 'anthropic'"
        "  UNION ALL SELECT 'workhorse', 'dev-ai')"
    ).fetchone()
    assert rows[0] == pytest.approx(6.00 + pricing.OPEN_WEIGHT["qwen/qwen3.8-27b"][0])
    only_local = con.execute("SELECT SUM(list_value('qwen', 1, 1, 1, 1, 1, 'omlx'))").fetchone()
    assert only_local[0] is None


def test_openrouter_ids_for_the_claude_table():
    assert pricing.openrouter_id("opus-5-5") == "anthropic/claude-opus-5.5"
    assert pricing.openrouter_id("opus-4") == "anthropic/claude-opus-4"
    assert pricing.exact_key("anthropic/claude-sonnet-4.6") == "sonnet-4-6"
    assert pricing.exact_key("anthropic/claude-opus-6") is None   # no family fallback here
