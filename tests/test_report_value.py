"""The daily report shows a list-price value next to tokens.

Value is Claude usage priced at API list prices, so a flat-fee plan can be
judged by what the same work would cost on the API. Groups with no Claude
usage (self-hosted models) show a dash, not $0.00.
"""

from __future__ import annotations

import re
import sqlite3

import report_html as rh
import report_morning as rm
from lib import ledger

NOW = ledger.iso_utc()
M = 1_000_000


def _insert(con, uid, harness, provider, model, out_tokens, org=None, sub=None):
    con.execute(
        "INSERT INTO usage (uid, ts, harness, provider, model, output_tokens,"
        " org, subscription, source, confidence)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', 'exact')",
        (uid, NOW, harness, provider, model, out_tokens, org, sub))


def _ledger(tmp_tokometer):
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    # Opus 5.5 output is $20/M, Sonnet 5.5 output $10/M
    _insert(con, "u1", "claude-code", "anthropic", "claude-opus-5-5", M, "Personal", "max")
    _insert(con, "u2", "claude-code", "anthropic", "claude-sonnet-5-5", M, "Client", "team")
    _insert(con, "u3", "omp", "omlx", "qwen-a", M, "Local", "self-hosted")
    con.commit()
    rh.set_period(None)
    return con


def _row(html, label):
    """The <tr> whose first cell mentions `label`."""
    for tr in re.findall(r"<tr>.*?</tr>", html, flags=re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)
        if cells and label in cells[0]:
            return cells
    raise AssertionError(f"no row for {label!r}")


def test_headline_has_a_value_column_per_harness(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    html = rh.headline(con)
    con.close()

    assert "<th>value</th>" in html
    assert "$20.00" in _row(html, "claude-code · Personal")
    assert "$10.00" in _row(html, "claude-code · Client")
    assert "—" in _row(html, "omp · Local")


def test_headline_card_shows_total_value(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    html = rh.headline(con)
    con.close()

    assert re.search(r'<div class="num">\$30\.00</div><div class="lbl">list value', html)


def test_accounts_table_values_each_plan(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    html = rh.accounts(con)
    con.close()

    assert "<th>value</th>" in html
    assert "$20.00" in _row(html, "Personal")
    assert "$10.00" in _row(html, "Client")
    assert "—" in _row(html, "Local")


def test_models_table_values_each_model(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    html = rh.models_table(con)
    con.close()

    assert "$20.00" in _row(html, "claude-opus-5-5")
    assert "$10.00" in _row(html, "claude-sonnet-5-5")
    assert "—" in _row(html, "qwen-a (omlx)")


def test_full_report_includes_the_models_table(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    html = rh.build(con)
    con.close()

    assert "Value by model" in html


def test_morning_markdown_carries_value_too(tmp_tokometer):
    con = _ledger(tmp_tokometer)
    md = rm.headline(con) + rm.accounts(con)
    con.close()

    assert "| value |" in md
    assert "$20.00" in md and "$10.00" in md
    assert "$30.00" in md   # headline total


def test_self_hosted_models_are_valued_at_open_weight_prices(tmp_tokometer):
    con = sqlite3.connect(str(tmp_tokometer / "ledger.db"))
    # Qwen3.8 Flash-Next output is $0.47/M at the OpenRouter median
    _insert(con, "u1", "omp", "omlx", "Qwen3.8-Flash-Next-oQ4e-mtp", M, "Local", "self-hosted")
    con.commit()
    rh.set_period(None)
    html = rh.headline(con)
    con.close()

    assert "$0.47" in _row(html, "omp · Local")
