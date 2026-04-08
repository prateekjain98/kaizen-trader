"""Tests for gap analysis fixes: regime gating, DCA scaling, OI composite, exchange flows."""

import time
import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

from tests.conftest import make_position, make_signal, _now_ms


# ─── Regime-Based Hard Gating ───────────────────────────────────────────────

class TestRegimeGate:
    def _reset_cache(self):
        from src.risk.regime_gate import _gate_cache
        _gate_cache.clear()

    def test_unknown_strategy_not_blocked(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="trending_up", trend_strength=50,
            volatility="high_vol", phase="markup",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                assert is_regime_blocked("unknown_strat") is False

    def test_mean_reversion_blocked_in_strong_trend(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="trending_up", trend_strength=40,  # ADX 40 > 35 threshold
            volatility="normal_vol", phase="markup",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                assert is_regime_blocked("mean_reversion") is True

    def test_mean_reversion_allowed_in_weak_trend(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="trending_up", trend_strength=20,  # ADX 20 < 35 threshold
            volatility="normal_vol", phase="unknown",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                assert is_regime_blocked("mean_reversion") is False

    def test_momentum_blocked_in_ranging_low_vol(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="ranging", trend_strength=15,
            volatility="low_vol", phase="unknown",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                assert is_regime_blocked("momentum_swing") is True

    def test_momentum_allowed_in_ranging_high_vol(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="ranging", trend_strength=15,
            volatility="high_vol", phase="unknown",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                assert is_regime_blocked("momentum_swing") is False

    def test_cache_works(self):
        from src.risk.regime_gate import is_regime_blocked
        self._reset_cache()
        mock_regime = MagicMock(
            trend="trending_up", trend_strength=40,
            volatility="normal_vol", phase="markup",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime) as mock:
            with patch("src.risk.regime_gate.log"):
                is_regime_blocked("mean_reversion")
                is_regime_blocked("mean_reversion")
                # classify_regime should be called once due to caching
                assert mock.call_count == 1

    def test_get_blocked_strategies(self):
        from src.risk.regime_gate import get_blocked_strategies, _gate_cache
        _gate_cache.clear()
        mock_regime = MagicMock(
            trend="trending_down", trend_strength=45,
            volatility="normal_vol", phase="markdown",
        )
        with patch("src.risk.regime_gate.classify_regime", return_value=mock_regime):
            with patch("src.risk.regime_gate.log"):
                blocked = get_blocked_strategies()
                assert "mean_reversion" in blocked
                assert "fear_greed_contrarian" in blocked


# ─── DCA Scaling-In ──────────────────────────────────────────────────────────

class TestDCAScaling:
    def test_scalp_always_full_size(self):
        from src.risk.scaling import get_initial_fraction, get_max_tranches
        assert get_initial_fraction("scalp") == 1.0
        assert get_max_tranches("scalp") == 1

    def test_swing_starts_at_50pct(self):
        from src.risk.scaling import get_initial_fraction, get_max_tranches
        assert get_initial_fraction("swing") == 0.50
        assert get_max_tranches("swing") == 3

    def test_no_tranche_for_scalp(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(tier="scalp")
        pos.tranche_count = 1
        pos.max_tranches = 1
        assert should_add_tranche(pos, 2000.0) is None

    def test_retrace_tranche_triggers_for_long(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(side="long", entry_price=100.0, tier="swing")
        pos.tranche_count = 1
        pos.max_tranches = 3

        # Price needs to drop 1.5% from entry (100 -> 98.5)
        result = should_add_tranche(pos, 98.0)  # below 98.5
        assert result is not None
        assert result["fraction"] == 0.25
        assert "retrace" in result["reason"]

    def test_retrace_tranche_not_triggered_if_price_too_high(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(side="long", entry_price=100.0, tier="swing")
        pos.tranche_count = 1
        pos.max_tranches = 3

        result = should_add_tranche(pos, 99.5)  # above 98.5
        assert result is None

    def test_confirm_tranche_triggers_for_long(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(side="long", entry_price=100.0, tier="swing")
        pos.tranche_count = 2  # already had retrace tranche
        pos.max_tranches = 3

        # Price needs to rise 1% from entry (100 -> 101)
        result = should_add_tranche(pos, 101.5)  # above 101
        assert result is not None
        assert result["fraction"] == 0.25
        assert "confirmation" in result["reason"]

    def test_no_more_tranches_when_maxed(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(tier="swing")
        pos.tranche_count = 3
        pos.max_tranches = 3
        assert should_add_tranche(pos, 2000.0) is None

    def test_short_retrace_tranche(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(side="short", entry_price=100.0, tier="swing")
        pos.tranche_count = 1
        pos.max_tranches = 3

        # For shorts, retrace = price goes UP (against short)
        result = should_add_tranche(pos, 102.0)  # above 101.5 threshold
        assert result is not None
        assert "retrace" in result["reason"]

    def test_short_confirm_tranche(self):
        from src.risk.scaling import should_add_tranche
        pos = make_position(side="short", entry_price=100.0, tier="swing")
        pos.tranche_count = 2
        pos.max_tranches = 3

        # For shorts, confirmation = price goes DOWN (in our favor)
        result = should_add_tranche(pos, 98.5)  # below 99.0 threshold
        assert result is not None
        assert "confirmation" in result["reason"]


# ─── OI + Funding Composite ─────────────────────────────────────────────────

class TestOIFundingComposite:
    def test_no_derivatives_returns_zero(self):
        from src.qualification.scorer import _oi_funding_composite
        sig = make_signal(side="long")
        assert _oi_funding_composite(sig, None) == 0

    def test_high_oi_positive_funding_penalizes_longs(self):
        from src.qualification.scorer import _oi_funding_composite
        sig = make_signal(side="long")
        deriv = MagicMock(
            open_interest_usd=1_000_000_000,  # $1B OI
            funding_rate=0.001,  # very positive
        )
        adj = _oi_funding_composite(sig, deriv)
        assert adj < 0  # should penalize longs

    def test_high_oi_negative_funding_boosts_longs(self):
        from src.qualification.scorer import _oi_funding_composite
        sig = make_signal(side="long")
        deriv = MagicMock(
            open_interest_usd=1_000_000_000,
            funding_rate=-0.001,  # very negative
        )
        adj = _oi_funding_composite(sig, deriv)
        assert adj > 0  # should boost longs (crowded shorts)

    def test_low_oi_returns_small_adjustment(self):
        from src.qualification.scorer import _oi_funding_composite
        sig = make_signal(side="long")
        deriv = MagicMock(
            open_interest_usd=100_000_000,  # only $100M
            funding_rate=0.0001,  # normal funding
        )
        adj = _oi_funding_composite(sig, deriv)
        assert abs(adj) <= 3  # small or zero


# ─── Exchange Flow Adjustment ────────────────────────────────────────────────

class TestExchangeFlow:
    def test_large_outflow_boosts_longs(self):
        from src.qualification.scorer import _exchange_flow_adjustment
        sig = make_signal(side="long")
        mock_flows = {
            "net_flow_usd": 30_000_000,  # $30M net outflow
            "symbols_tracked": 5,
        }
        with patch("src.strategies.whale_tracker.get_net_exchange_flow", return_value=mock_flows):
            adj = _exchange_flow_adjustment(sig)
            assert adj > 0

    def test_large_inflow_penalizes_longs(self):
        from src.qualification.scorer import _exchange_flow_adjustment
        sig = make_signal(side="long")
        mock_flows = {
            "net_flow_usd": -30_000_000,  # $30M net inflow (sell pressure)
            "symbols_tracked": 5,
        }
        with patch("src.strategies.whale_tracker.get_net_exchange_flow", return_value=mock_flows):
            adj = _exchange_flow_adjustment(sig)
            assert adj < 0

    def test_insufficient_data_returns_zero(self):
        from src.qualification.scorer import _exchange_flow_adjustment
        sig = make_signal(side="long")
        mock_flows = {
            "net_flow_usd": 50_000_000,
            "symbols_tracked": 1,  # less than 2
        }
        with patch("src.strategies.whale_tracker.get_net_exchange_flow", return_value=mock_flows):
            adj = _exchange_flow_adjustment(sig)
            assert adj == 0

    def test_whale_tracker_aggregation(self):
        from src.strategies.whale_tracker import get_net_exchange_flow, on_whale_transfer
        # Clear existing state
        from src.strategies.whale_tracker import _flow_windows
        _flow_windows.clear()

        # Simulate whale transfers
        on_whale_transfer({
            "symbol": "BTC", "amount_usd": 10_000_000,
            "to_type": "exchange", "from_type": "unknown_wallet",
        })
        on_whale_transfer({
            "symbol": "ETH", "amount_usd": 20_000_000,
            "to_type": "unknown_wallet", "from_type": "exchange",
        })

        flows = get_net_exchange_flow()
        assert flows["symbols_tracked"] == 2
        assert flows["total_inflow_usd"] == 10_000_000
        assert flows["total_outflow_usd"] == 20_000_000
        assert flows["net_flow_usd"] == 10_000_000  # net outflow (bullish)
