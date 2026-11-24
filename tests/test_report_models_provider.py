"""report_html.chart_models labels a model with its provider.

Self-hosted servers serve the same model names under different providers
(dev-ai vLLM, oMLX, llama.cpp), so the model alone does not say which server
did the work. Anthropic is left off: claude-* names already say it.
"""

from __future__ import annotations

import sqlite3

import report_html as rh
from lib import ledger

NOW = ledger.iso_utc()


def _insert(con, uid, harness, provider, model, tokens):
    con.execute(
        "INSERT INTO usage (uid, ts, harness, provider, model, input_tokens,"
        " source, confidence) VALUES (?, ?, ?, ?, ?, ?, 'test', 'exact')",
        (uid, NOW, harness, provider, model, tokens))


def test_models_chart_names_the_provider_for_self_hosted_models(tmp_tokometer):
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    _insert(con, "u1", "omp", "omlx", "qwen-a", 3000)
    _insert(con, "u2", "omp", "dev-ai", "qwen-a", 2000)
    _insert(con, "u3", "claude-code", "anthropic", "claude-opus-5-5", 1000)
    con.commit()

    svg = rh.chart_models(con)
    con.close()

    assert "qwen-a (omlx)" in svg
    assert "qwen-a (dev-ai)" in svg
    assert "claude-opus-5-5" in svg and "(anthropic)" not in svg


def test_long_model_names_keep_the_provider(tmp_tokometer):
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    _insert(con, "u1", "omp", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", 3000)
    con.commit()

    svg = rh.chart_models(con)
    con.close()

    assert "(omlx)" in svg
    assert "Qwen3.8-Flash-Next" in svg


def test_morning_report_names_the_provider_per_model(tmp_tokometer):
    import report_morning as rm
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    _insert(con, "u1", "omp", "omlx", "qwen-a", 3000)
    _insert(con, "u2", "omp", "dev-ai", "qwen-a", 2000)
    _insert(con, "u3", "claude-code", "anthropic", "claude-opus-5-5", 1000)
    con.commit()

    md = rm.headline(con)
    con.close()

    assert "qwen-a (omlx) --" in md and "qwen-a (dev-ai) --" in md
    assert "claude-opus-5-5 --" in md
