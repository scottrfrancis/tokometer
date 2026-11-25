"""price_check.py rechecks lib/pricing.py against OpenRouter once a month.

daily.sh calls it every day; it does nothing until 30 days have passed since
the last good check, then compares every price in the table with OpenRouter
and records any that moved, plus Claude models OpenRouter lists that the
table lacks. The morning report shows what it found.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

import price_check as pc
import report_html as rh
from lib import pricing

NOW = dt.datetime(2026, 12, 1, 4, 0, tzinfo=dt.timezone.utc)


def _per_token(rates):
    """OpenRouter quotes dollars per token as strings."""
    i, o, c = rates
    return {"prompt": f"{i / 1e6:.12f}", "completion": f"{o / 1e6:.12f}",
            "input_cache_read": f"{c / 1e6:.12f}"}


class FakeOpenRouter:
    """Serves a catalog and per-model endpoint lists that agree with our table."""

    def __init__(self):
        self.catalog = [{"id": pricing.openrouter_id(k), "pricing": _per_token(v)}
                        for k, v in pricing.PRICES.items()]
        self.endpoints = {mid: [_per_token(v)] * 3 for mid, v in pricing.OPEN_WEIGHT.items()}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url == pc.CATALOG_URL:
            return {"data": self.catalog}
        for mid, eps in self.endpoints.items():
            if url == pc.endpoints_url(mid):
                return {"data": {"endpoints": [{"pricing": p} for p in eps]}}
        raise AssertionError(f"unexpected url {url}")


def test_no_drift_when_openrouter_agrees(tmp_path):
    state = pc.run(tmp_path / "price_check.json", FakeOpenRouter(), NOW)

    assert state["ok"] is True
    assert state["drift"] == [] and state["new_claude"] == []
    assert json.loads((tmp_path / "price_check.json").read_text())["checked_at"] == "2026-12-01"


def test_open_weight_prices_use_the_median_provider():
    eps = [{"pricing": _per_token(r)} for r in
           [(0.10, 1.00, 0.0), (0.20, 2.00, 0.05), (0.90, 9.00, 0.08)]]
    # a provider that lists no cache price bills cache reads as input
    assert pc.median_rates(eps) == pytest.approx((0.20, 2.00, 0.08))


def test_a_price_that_moves_more_than_the_tolerance_is_drift(tmp_path):
    fake = FakeOpenRouter()
    fake.endpoints["qwen/qwen3.8-27b"] = [_per_token((0.1875, 3.00, 0.05))] * 3
    fake.catalog = [dict(e, pricing=_per_token((4.20, 20.0, 0.20)))
                    if e["id"] == "anthropic/claude-opus-5.5" else e for e in fake.catalog]

    state = pc.run(tmp_path / "s.json", fake, NOW)

    drift = {(d["id"], d["field"]): (d["ours"], d["theirs"]) for d in state["drift"]}
    assert drift == {("qwen/qwen3.8-27b", "output"): (1.99, pytest.approx(3.00))}


def test_new_claude_models_are_listed(tmp_path):
    fake = FakeOpenRouter()
    fake.catalog.append({"id": "anthropic/claude-opus-6", "pricing": _per_token((4, 20, 0.2))})
    fake.catalog.append({"id": "anthropic/claude-opus-5.5:batch", "pricing": _per_token((2, 10, 0.1))})

    state = pc.run(tmp_path / "s.json", fake, NOW)

    assert state["new_claude"] == ["anthropic/claude-opus-6"]


def test_skips_until_a_month_has_passed(tmp_path):
    path = tmp_path / "s.json"
    pc.run(path, FakeOpenRouter(), NOW)
    fake = FakeOpenRouter()

    pc.run(path, fake, NOW + dt.timedelta(days=29))
    assert fake.calls == []
    pc.run(path, fake, NOW + dt.timedelta(days=30))
    assert fake.calls


def test_a_failed_fetch_is_recorded_and_retried_next_run(tmp_path):
    path = tmp_path / "s.json"
    pc.run(path, FakeOpenRouter(), NOW)

    def offline(url):
        raise OSError("network down")

    later = NOW + dt.timedelta(days=31)
    state = pc.run(path, offline, later)
    assert state["ok"] is False and "network down" in state["error"]
    assert state["checked_at"] == "2026-12-01"     # the last good check is kept

    fake = FakeOpenRouter()
    pc.run(path, fake, later + dt.timedelta(days=1))
    assert fake.calls                                # retried, not skipped


# ---- the morning report surfaces the result

def _advisory(tmp_path, monkeypatch, state, today=NOW.date()):
    path = tmp_path / "price_check.json"
    if state is not None:
        path.write_text(json.dumps(state))
    monkeypatch.setattr(rh, "PRICE_STATE", str(path))
    return rh.price_advisory(today)


def test_report_is_quiet_after_a_clean_recent_check(tmp_path, monkeypatch):
    state = {"ok": True, "checked_at": "2026-11-25", "drift": [], "new_claude": []}
    assert _advisory(tmp_path, monkeypatch, state) == ""


def test_report_lists_drift_and_new_models(tmp_path, monkeypatch):
    state = {"ok": True, "checked_at": "2026-12-01", "new_claude": ["anthropic/claude-opus-6"],
             "drift": [{"id": "qwen/qwen3.8-27b", "field": "output", "ours": 1.99, "theirs": 3.0}]}
    html = _advisory(tmp_path, monkeypatch, state)
    assert "qwen/qwen3.8-27b" in html and "$1.99" in html and "$3.00" in html
    assert "anthropic/claude-opus-6" in html
    assert "lib/pricing.py" in html


def test_report_flags_a_check_that_keeps_failing(tmp_path, monkeypatch):
    state = {"ok": False, "checked_at": "2026-10-01", "error": "network down", "drift": [],
             "new_claude": []}
    html = _advisory(tmp_path, monkeypatch, state)
    assert "network down" in html and "2026-10-01" in html


def test_report_flags_a_check_that_never_ran(tmp_path, monkeypatch):
    assert "price_check.py" in _advisory(tmp_path, monkeypatch, None)


def test_daily_runs_the_check_before_the_report():
    daily = (pc.__file__.rsplit("/", 1)[0] + "/daily.sh")
    text = open(daily).read()
    assert "price_check.py" in text
    assert text.index("price_check.py") < text.index("report_html.py")
