"""Tests for Tier 2 gap fixes: options max pain, leverage bracket analysis."""

import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import make_signal


# ─── Options Max Pain ─────────────────────────────────────────────────────────

class TestMaxPainComputation:
    def test_compute_max_pain_basic(self):
        from src.signals.options import _compute_max_pain
        instruments = [
            {"instrument_name": "BTC-28MAR25-90000-C", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-95000-C", "open_interest": 200},
            {"instrument_name": "BTC-28MAR25-100000-C", "open_interest": 300},
            {"instrument_name": "BTC-28MAR25-90000-P", "open_interest": 300},
            {"instrument_name": "BTC-28MAR25-95000-P", "open_interest": 200},
            {"instrument_name": "BTC-28MAR25-100000-P", "open_interest": 100},
        ]
        mp = _compute_max_pain(instruments)
        assert mp is not None
        assert 85000 <= mp <= 105000

    def test_max_pain_symmetric_oi(self):
        """With symmetric OI, max pain should be near the middle strike."""
        from src.signals.options import _compute_max_pain
        instruments = [
            {"instrument_name": "BTC-28MAR25-90000-C", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-95000-C", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-100000-C", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-90000-P", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-95000-P", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-100000-P", "open_interest": 100},
        ]
        mp = _compute_max_pain(instruments)
        assert mp == 95000  # middle strike minimizes total payout

    def test_max_pain_empty_instruments(self):
        from src.signals.options import _compute_max_pain
        assert _compute_max_pain([]) is None

    def test_max_pain_insufficient_strikes(self):
        from src.signals.options import _compute_max_pain
        instruments = [
            {"instrument_name": "BTC-28MAR25-90000-C", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-90000-P", "open_interest": 100},
        ]
        # Only 1 strike — needs at least 3
        assert _compute_max_pain(instruments) is None

    def test_max_pain_skewed_put_oi(self):
        """Heavy put OI at low strikes pulls max pain down."""
        from src.signals.options import _compute_max_pain
        instruments = [
            {"instrument_name": "BTC-28MAR25-80000-C", "open_interest": 50},
            {"instrument_name": "BTC-28MAR25-90000-C", "open_interest": 50},
            {"instrument_name": "BTC-28MAR25-100000-C", "open_interest": 50},
            {"instrument_name": "BTC-28MAR25-80000-P", "open_interest": 500},
            {"instrument_name": "BTC-28MAR25-90000-P", "open_interest": 100},
            {"instrument_name": "BTC-28MAR25-100000-P", "open_interest": 10},
        ]
        mp = _compute_max_pain(instruments)
        assert mp is not None
        # Heavy put OI at 80000 means max pain should be at or above 80000
        # (settling above 80000 makes those puts expire worthless)
        assert mp >= 80000

    def test_extract_strike(self):
        from src.signals.options import _extract_strike
        assert _extract_strike("BTC-28MAR25-85000-C") == 85000.0
        assert _extract_strike("ETH-28MAR25-3500-P") == 3500.0
        assert _extract_strike("INVALID") is None
        assert _extract_strike("BTC-28MAR25") is None


class TestMaxPainScoring:
    def test_spot_far_above_max_pain_penalizes_longs(self):
        from src.qualification.scorer import _options_adjustment
        sig = make_signal(side="long")
        opts = MagicMock(
            put_call_ratio=1.0, skew_25d=0,
            spot_to_max_pain_pct=8.0,  # spot 8% above max pain
        )
        adj = _options_adjustment(sig, opts)
        assert adj < 0  # headwind for longs

    def test_spot_far_below_max_pain_boosts_longs(self):
        from src.qualification.scorer import _options_adjustment
        sig = make_signal(side="long")
        opts = MagicMock(
            put_call_ratio=1.0, skew_25d=0,
            spot_to_max_pain_pct=-8.0,  # spot 8% below max pain
        )
        adj = _options_adjustment(sig, opts)
        assert adj > 0  # tailwind for longs

    def test_spot_near_max_pain_no_extra_adjustment(self):
        from src.qualification.scorer import _options_adjustment
        sig = make_signal(side="long")
        opts = MagicMock(
            put_call_ratio=1.0, skew_25d=0,
            spot_to_max_pain_pct=2.0,  # close to max pain
        )
        adj = _options_adjustment(sig, opts)
        assert adj == 0  # no max pain effect, and neutral p/c + skew

    def test_no_max_pain_data_still_works(self):
        from src.qualification.scorer import _options_adjustment
        sig = make_signal(side="long")
        opts = MagicMock(
            put_call_ratio=1.5,  # bearish
            skew_25d=-15,  # fear
            spot_to_max_pain_pct=None,
        )
        adj = _options_adjustment(sig, opts)
        assert adj < 0  # bearish from p/c + skew


# ─── Leverage Bracket Analysis ─────────────────────────────────────────────────

class TestLeverageBracketData:
    def test_leverage_profile_dataclass(self):
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        bracket = LeverageBracket(
            bracket="global", long_ratio=0.6, short_ratio=0.4,
            long_short_ratio=1.5,
        )
        profile = LeverageProfile(
            symbol="BTC", brackets=[bracket],
            high_leverage_long_pct=5.0, high_leverage_short_pct=2.0,
            top_trader_long_ratio=0.55, top_trader_short_ratio=0.45,
        )
        assert profile.symbol == "BTC"
        assert len(profile.brackets) == 1
        assert profile.high_leverage_long_pct == 5.0

    def test_derivatives_data_includes_leverage(self):
        from src.signals.derivatives import DerivativesData, LeverageProfile, LeverageBracket
        lp = LeverageProfile(
            symbol="BTC", brackets=[],
            high_leverage_long_pct=0, high_leverage_short_pct=0,
            top_trader_long_ratio=0.5, top_trader_short_ratio=0.5,
        )
        data = DerivativesData(
            symbol="BTC", futures_basis_pct=0.1, open_interest_usd=1_000_000_000,
            funding_rate=0.0001, mark_price=95000, index_price=94900,
            leverage_profile=lp,
        )
        assert data.leverage_profile is not None
        assert data.leverage_profile.symbol == "BTC"


class TestLeverageScoring:
    def test_no_leverage_data_returns_zero(self):
        from src.qualification.scorer import _leverage_profile_adjustment
        sig = make_signal(side="long")
        deriv = MagicMock(leverage_profile=None)
        assert _leverage_profile_adjustment(sig, deriv) == 0

    def test_no_deriv_returns_zero(self):
        from src.qualification.scorer import _leverage_profile_adjustment
        sig = make_signal(side="long")
        assert _leverage_profile_adjustment(sig, None) == 0

    def test_smart_money_short_penalizes_longs(self):
        """When top traders are short but retail is long → bearish for longs."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.6, short_ratio=0.4,
                                long_short_ratio=1.5),
            ],
            high_leverage_long_pct=8.0,  # lots of retail leverage on long side
            high_leverage_short_pct=1.0,
            top_trader_long_ratio=0.40,  # top traders more short
            top_trader_short_ratio=0.60,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert adj < 0  # penalize longs

    def test_smart_money_long_boosts_longs(self):
        """When top traders are long but retail is short → bullish for longs."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.4, short_ratio=0.6,
                                long_short_ratio=0.67),
            ],
            high_leverage_long_pct=1.0,
            high_leverage_short_pct=8.0,  # lots of retail leverage on short side
            top_trader_long_ratio=0.60,  # top traders more long
            top_trader_short_ratio=0.40,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert adj > 0  # boost longs

    def test_extreme_global_long_crowding(self):
        """When >65% of global accounts are long → liq risk, penalize longs."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.70, short_ratio=0.30,
                                long_short_ratio=2.33),
            ],
            high_leverage_long_pct=3.0,
            high_leverage_short_pct=1.0,
            top_trader_long_ratio=0.50,  # neutral top traders
            top_trader_short_ratio=0.50,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert adj < 0  # crowded longs = liq risk

    def test_extreme_global_short_crowding_boosts_longs(self):
        """When >65% of global accounts are short → squeeze risk, boost longs."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.30, short_ratio=0.70,
                                long_short_ratio=0.43),
            ],
            high_leverage_long_pct=1.0,
            high_leverage_short_pct=3.0,
            top_trader_long_ratio=0.50,
            top_trader_short_ratio=0.50,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert adj > 0  # squeeze risk = good for longs

    def test_balanced_positioning_returns_zero(self):
        """When positioning is balanced, no adjustment."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.52, short_ratio=0.48,
                                long_short_ratio=1.08),
            ],
            high_leverage_long_pct=2.0,
            high_leverage_short_pct=2.0,
            top_trader_long_ratio=0.50,
            top_trader_short_ratio=0.50,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert adj == 0

    def test_adjustment_clamped(self):
        """Adjustment should be clamped to [-6, 6]."""
        from src.qualification.scorer import _leverage_profile_adjustment
        from src.signals.derivatives import LeverageProfile, LeverageBracket
        sig = make_signal(side="long")
        # Extreme scenario: smart money very short + global very long
        lp = LeverageProfile(
            symbol="BTC",
            brackets=[
                LeverageBracket(bracket="global", long_ratio=0.80, short_ratio=0.20,
                                long_short_ratio=4.0),
            ],
            high_leverage_long_pct=15.0,
            high_leverage_short_pct=0.5,
            top_trader_long_ratio=0.30,
            top_trader_short_ratio=0.70,
        )
        deriv = MagicMock(leverage_profile=lp)
        adj = _leverage_profile_adjustment(sig, deriv)
        assert -6 <= adj <= 6
