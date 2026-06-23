"""Delta-neutral funding-capture opportunity scanner (#1). Default-OFF guard."""

import os

from src.strategies.funding_neutral import (
    is_enabled, find_funding_neutral_opportunities, NeutralOpportunity,
)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_FUNDING_CARRY_NEUTRAL", raising=False)
    assert is_enabled() is False


def test_enabled_only_with_explicit_env(monkeypatch):
    monkeypatch.setenv("ENABLE_FUNDING_CARRY_NEUTRAL", "true")
    assert is_enabled() is True


def test_high_positive_funding_is_recommended_short_perp():
    # 0.05%/8h -> 0.15%/day -> ~54.75% APR, break-even ~2.7 days
    opps = find_funding_neutral_opportunities({"SYN": 0.0005})
    o = opps[0]
    assert o.perp_side == "short"        # positive funding -> short perp to receive it
    assert o.recommended is True
    assert o.gross_apr > 0.5
    assert o.breakeven_days < 3


def test_tiny_funding_not_recommended():
    # 0.005%/8h -> ~5.5% APR (< 8% floor), break-even ~27 days (> 10)
    opps = find_funding_neutral_opportunities({"BTC": 0.00005})
    assert opps[0].recommended is False


def test_negative_funding_is_long_perp():
    opps = find_funding_neutral_opportunities({"X": -0.0006})
    assert opps[0].perp_side == "long"
    assert opps[0].gross_apr > 0  # magnitude-based


def test_sorted_by_apr_desc():
    opps = find_funding_neutral_opportunities({"A": 0.0001, "B": 0.0006, "C": 0.0003})
    assert [o.symbol for o in opps] == ["B", "C", "A"]
