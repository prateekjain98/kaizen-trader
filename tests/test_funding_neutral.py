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


def test_liquidity_gate_passes_deep_tight_market():
    from src.strategies.funding_neutral import passes_liquidity_gate
    # SKHYNIX-like: $1012M vol, 0.017% spread
    assert passes_liquidity_gate(1012e6, 0.017) is True


def test_liquidity_gate_blocks_thin_market():
    from src.strategies.funding_neutral import passes_liquidity_gate
    # EBAY-like: $1M vol -> blocked regardless of tight spread
    assert passes_liquidity_gate(1e6, 0.009) is False


def test_liquidity_gate_blocks_wide_spread():
    from src.strategies.funding_neutral import passes_liquidity_gate
    # deep but wide (HYUNDAI-like 0.187% spread on borderline vol)
    assert passes_liquidity_gate(60e6, 0.19) is False
