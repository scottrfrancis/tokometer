"""TOKOMETER_PROVIDER_ALIASES: record a client's generic server name as the server's own.

OpenClaw and Open WebUI call dev-ai's vLLM as "vllm"; OMP calls it "dev-ai".
Aliasing puts all three under one provider, which is also the name the price
table's server aliases use.
"""

from __future__ import annotations

import pytest

from lib import ledger


@pytest.mark.parametrize("raw, expected", [
    ("vllm=dev-ai", {"vllm": "dev-ai"}),
    ("vllm=dev-ai other=studio", {"vllm": "dev-ai", "other": "studio"}),
    ("vllm=dev-ai, other=studio", {"vllm": "dev-ai", "other": "studio"}),
    ("", {}),
])
def test_aliases_are_read_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("TOKOMETER_PROVIDER_ALIASES", raw)

    assert ledger.provider_aliases() == expected


@pytest.mark.parametrize("raw", ["vllm", "vllm=", "=dev-ai"])
def test_a_malformed_alias_fails_loudly(monkeypatch, raw):
    monkeypatch.setenv("TOKOMETER_PROVIDER_ALIASES", raw)

    with pytest.raises(ValueError, match="TOKOMETER_PROVIDER_ALIASES"):
        ledger.provider_aliases()
