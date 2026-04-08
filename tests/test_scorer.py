"""Tests for the multi-signal qualification scorer."""

import pytest

from src.qualification.scorer import (
    _news_adjustment, _social_adjustment,
    _context_adjustment, _fear_greed_adjustment, qualify,
    _cvd_adjustment, _regime_adjustment, _options_adjustment,
    _derivatives_adjustment, _stablecoin_adjustment, _unlock_risk_adjustment,
)
from src.utils.safe_math import safe_score
from src.indicators.cvd import CVDSnapshot
from src.indicators.regime import RegimeSnapshot
from src.signals.news import NewsSentiment
from src.signals.options import OptionsSentiment
from src.signals.stablecoin import StablecoinFlows
from src.signals.derivatives import DerivativesData
from src.signals.social import SocialSentiment
from src.types import ScannerConfig, MarketContext
from tests.conftest import make_signal


# ── safe_score (was _clamp) ────────────────────────────────────────────────

class TestClamp:
    def test_within_range(self):
        assert safe_score(5, 0, 10) == 5

    def test_below_min(self):
        assert safe_score(-5, 0, 10) == 0

    def test_above_max(self):
        assert safe_score(15, 0, 10) == 10

    def test_at_boundaries(self):
        assert safe_score(0, 0, 10) == 0
        assert safe_score(10, 0, 10) == 10


# ── _news_adjustment ───────────────────────────────────────────────────────

class TestNewsAdjustment:
    def test_none_news(self):
        signal = make_signal(side="long")
        assert _news_adjustment(signal, None) == 0

    def test_bullish_news_long(self):
        signal = make_signal(side="long")
        news = NewsSentiment(symbol="ETH", score=0.8, mention_count=5,
                             top_headlines=[], velocity_ratio=1.0, sampled_at=0)
        adj = _news_adjustment(signal, news)
        # direction_match = 0.8, velocity = 0 (ratio < 2)
        # adj = clamp(0.8 * 12 + 0, -15, 15) = clamp(9.6, -15, 15) = 9.6
        assert abs(adj - 9.6) < 0.01

    def test_bullish_news_short_is_negative(self):
        signal = make_signal(side="short")
        news = NewsSentiment(symbol="ETH", score=0.8, mention_count=5,
                             top_headlines=[], velocity_ratio=1.0, sampled_at=0)
        adj = _news_adjustment(signal, news)
        # direction_match = -0.8 for short
        # adj = clamp(-0.8 * 12, -15, 15) = clamp(-9.6, -15, 15) = -9.6
        assert abs(adj - (-9.6)) < 0.01

    def test_high_velocity_adds_bonus(self):
        signal = make_signal(side="long")
        news = NewsSentiment(symbol="ETH", score=0.5, mention_count=10,
                             top_headlines=[], velocity_ratio=4.0, sampled_at=0)
        adj = _news_adjustment(signal, news)
        # direction_match = 0.5, velocity = min(5, (4-2)*2.5) = min(5, 5) = 5
        # adj = clamp(0.5*12 + 5, -15, 15) = clamp(11, -15, 15) = 11
        assert abs(adj - 11.0) < 0.01

    def test_clamped_to_bounds(self):
        signal = make_signal(side="long")
        news = NewsSentiment(symbol="ETH", score=1.0, mention_count=10,
                             top_headlines=[], velocity_ratio=10.0, sampled_at=0)
        adj = _news_adjustment(signal, news)
        assert adj == 15  # clamped to max


# ── _social_adjustment ─────────────────────────────────────────────────────

class TestSocialAdjustment:
    def test_none_social(self):
        assert _social_adjustment(make_signal(), None) == 0

    def test_high_galaxy_long(self):
        signal = make_signal(side="long")
        social = SocialSentiment(
            symbol="ETH", galaxy_score=80, alt_rank=10,
            social_volume=1000, velocity_multiple=2.0,
            sentiment=0.7, sampled_at=0,
        )
        adj = _social_adjustment(signal, social)
        # galaxy >= 70 -> +5, velocity >= 2 -> +3 = 8
        assert adj == 8

    def test_high_galaxy_short(self):
        signal = make_signal(side="short")
        social = SocialSentiment(
            symbol="ETH", galaxy_score=80, alt_rank=10,
            social_volume=1000, velocity_multiple=2.0,
            sentiment=0.7, sampled_at=0,
        )
        adj = _social_adjustment(signal, social)
        # galaxy >= 70 -> +5, velocity >= 2 -> +3 = 8 (no side inversion in new formula)
        assert adj == 8

    def test_low_galaxy_long_is_negative(self):
        signal = make_signal(side="long")
        social = SocialSentiment(
            symbol="ETH", galaxy_score=20, alt_rank=50,
            social_volume=100, velocity_multiple=1.0,
            sentiment=0.3, sampled_at=0,
        )
        adj = _social_adjustment(signal, social)
        # galaxy <= 30 -> -5
        assert adj == -5

    def test_velocity_bonus(self):
        signal = make_signal(side="long")
        social = SocialSentiment(
            symbol="ETH", galaxy_score=50, alt_rank=25,
            social_volume=500, velocity_multiple=5.0,
            sentiment=0.5, sampled_at=0,
        )
        adj = _social_adjustment(signal, social)
        # galaxy in 31-69 -> 0, velocity >= 3 -> +7 = 7
        assert adj == 7


# ── _context_adjustment ───────────────────────────────────────────────────

class TestContextAdjustment:
    def test_bull_long(self, bull_ctx):
        adj = _context_adjustment(make_signal(side="long"), bull_ctx)
        assert adj == 8

    def test_bull_short(self, bull_ctx):
        adj = _context_adjustment(make_signal(side="short"), bull_ctx)
        assert adj == -5

    def test_bear_long(self, bear_ctx):
        adj = _context_adjustment(make_signal(side="long"), bear_ctx)
        # bear long = -8, btc_dominance = 55 (not > 55, so no penalty)
        assert adj == -8

    def test_bear_short(self, bear_ctx):
        adj = _context_adjustment(make_signal(side="short"), bear_ctx)
        assert adj == 8

    def test_extreme_greed_long(self):
        ctx = MarketContext(phase="extreme_greed", btc_dominance=45,
                           fear_greed_index=90, total_market_cap_change_d1=5,
                           timestamp=0)
        adj = _context_adjustment(make_signal(side="long"), ctx)
        assert adj == -5

    def test_extreme_fear_long(self):
        ctx = MarketContext(phase="extreme_fear", btc_dominance=45,
                           fear_greed_index=10, total_market_cap_change_d1=-5,
                           timestamp=0)
        adj = _context_adjustment(make_signal(side="long"), ctx)
        assert adj == 3

    def test_btc_dominance_penalty_non_btc_long(self):
        ctx = MarketContext(phase="neutral", btc_dominance=60,
                           fear_greed_index=50, total_market_cap_change_d1=0,
                           timestamp=0)
        adj = _context_adjustment(make_signal(symbol="ETH", side="long"), ctx)
        # neutral -> 0, btc_dom > 55 + long + not BTC -> -3
        assert adj == -3

    def test_btc_dominance_no_penalty_for_btc(self):
        ctx = MarketContext(phase="neutral", btc_dominance=60,
                           fear_greed_index=50, total_market_cap_change_d1=0,
                           timestamp=0)
        adj = _context_adjustment(make_signal(symbol="BTC", side="long"), ctx)
        assert adj == 0


# ── _fear_greed_adjustment ────────────────────────────────────────────────

class TestFearGreedAdjustment:
    def test_long_extreme_fear(self):
        assert _fear_greed_adjustment(make_signal(side="long"), 20) == 6

    def test_long_extreme_greed(self):
        assert _fear_greed_adjustment(make_signal(side="long"), 80) == -5

    def test_long_neutral(self):
        assert _fear_greed_adjustment(make_signal(side="long"), 50) == 0

    def test_short_extreme_greed(self):
        assert _fear_greed_adjustment(make_signal(side="short"), 75) == 6

    def test_short_extreme_fear(self):
        assert _fear_greed_adjustment(make_signal(side="short"), 20) == -5

    def test_short_neutral(self):
        assert _fear_greed_adjustment(make_signal(side="short"), 50) == 0


# ── qualify (integration) ─────────────────────────────────────────────────

class TestQualify:
    def test_basic_pass(self, neutral_ctx, config):
        signal = make_signal(score=70, tier="swing")
        result = qualify(signal, neutral_ctx, config)
        assert result.passed is True
        assert result.score >= config.min_qual_score_swing

    def test_basic_fail(self, neutral_ctx, config):
        signal = make_signal(score=30, tier="swing")
        result = qualify(signal, neutral_ctx, config)
        assert result.passed is False
        assert result.score < config.min_qual_score_swing

    def test_scalp_uses_scalp_threshold(self, neutral_ctx, config):
        signal = make_signal(score=50, tier="scalp")
        result = qualify(signal, neutral_ctx, config)
        assert result.passed is True  # 50 >= 45 (min_qual_score_scalp)

    def test_score_clamped_to_0_100(self, neutral_ctx, config):
        # Very high base score + positive adjustments shouldn't exceed 100
        signal = make_signal(score=95, tier="swing", side="long")
        ctx = MarketContext(phase="bull", btc_dominance=40,
                            fear_greed_index=20, total_market_cap_change_d1=5,
                            timestamp=0)
        result = qualify(signal, ctx, config)
        assert result.score <= 100

        # Very low base score + negative adjustments shouldn't go below 0
        signal = make_signal(score=5, tier="swing", side="long")
        ctx = MarketContext(phase="bear", btc_dominance=60,
                            fear_greed_index=80, total_market_cap_change_d1=-5,
                            timestamp=0)
        result = qualify(signal, ctx, config)
        assert result.score >= 0

    def test_breakdown_keys(self, neutral_ctx, config):
        signal = make_signal(score=65)
        result = qualify(signal, neutral_ctx, config)
        assert "base" in result.breakdown
        assert "news_adjustment" in result.breakdown
        assert "social_adjustment" in result.breakdown
        assert "context_adjustment" in result.breakdown
        assert "fear_greed_adjustment" in result.breakdown

    def test_reasoning_contains_base(self, neutral_ctx, config):
        signal = make_signal(score=65)
        result = qualify(signal, neutral_ctx, config)
        assert "base=65" in result.reasoning

    def test_with_all_adjustments(self, config):
        signal = make_signal(score=60, side="long", symbol="ETH")
        ctx = MarketContext(phase="bull", btc_dominance=45,
                            fear_greed_index=20, total_market_cap_change_d1=3,
                            timestamp=0)
        news = NewsSentiment(symbol="ETH", score=0.5, mention_count=5,
                             top_headlines=[], velocity_ratio=1.0, sampled_at=0)
        social = SocialSentiment(
            symbol="ETH", galaxy_score=70, alt_rank=15,
            social_volume=500, velocity_multiple=2.0,
            sentiment=0.6, sampled_at=0,
        )
        result = qualify(signal, ctx, config, news=news, social=social)
        # base=60 + news(6) + social(3.2) + ctx(8) + fgi(6) = 83.2
        assert result.passed is True
        assert result.score > 70

    def test_qualify_with_all_10_signals(self, config):
        signal = make_signal(score=60, side="long", symbol="ETH")
        ctx = MarketContext(phase="bull", btc_dominance=45,
                            fear_greed_index=50, total_market_cap_change_d1=1,
                            timestamp=0)
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=-0.5,
                          buy_volume_1m=1000, sell_volume_1m=400)
        regime = RegimeSnapshot(ts=0, trend="trending_up", trend_strength=40,
                                volatility="normal_vol")
        options = OptionsSentiment(symbol="ETH", put_call_ratio=0.6,
                                  total_put_oi=100, total_call_oi=200,
                                  implied_vol_avg=0.5, skew_25d=5.0)
        deriv = DerivativesData(symbol="ETH", futures_basis_pct=0.1,
                                open_interest_usd=1e9, funding_rate=0.0001,
                                mark_price=3000, index_price=2999)
        stable = StablecoinFlows(total_stablecoin_mcap=150e9,
                                 mcap_change_24h_pct=0.2, mcap_change_7d_pct=0.5,
                                 usdt_dominance=0.65, usdt_mcap=100e9, usdc_mcap=50e9)
        result = qualify(signal, ctx, config, cvd=cvd, regime=regime,
                         options=options, derivatives=deriv, stablecoin=stable,
                         has_unlock_risk=False)
        assert result.passed is True
        assert "cvd" in result.breakdown or result.breakdown.get("cvd_adjustment", 0) != 0


# ── _cvd_adjustment ──────────────────────────────────────────────────────

class TestCVDAdjustment:
    def test_none_cvd(self):
        assert _cvd_adjustment(make_signal(side="long"), None) == 0

    def test_bearish_divergence_penalizes_long(self):
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=0.5)
        adj = _cvd_adjustment(make_signal(side="long"), cvd)
        assert adj == -6

    def test_bullish_divergence_boosts_long(self):
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=-0.5)
        adj = _cvd_adjustment(make_signal(side="long"), cvd)
        assert adj == 5

    def test_bearish_divergence_boosts_short(self):
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=0.5)
        adj = _cvd_adjustment(make_signal(side="short"), cvd)
        assert adj == 5

    def test_buy_pressure_confirms_long(self):
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=0,
                          buy_volume_1m=2100, sell_volume_1m=1000)
        adj = _cvd_adjustment(make_signal(side="long"), cvd)
        assert adj == 3  # ratio > 2.0 confirms long

    def test_sell_pressure_confirms_short(self):
        cvd = CVDSnapshot(symbol="ETH", ts=0, cvd=100, divergence_score=0,
                          buy_volume_1m=400, sell_volume_1m=1000)
        adj = _cvd_adjustment(make_signal(side="short"), cvd)
        assert adj == 3  # ratio < 0.5 confirms short


# ── _regime_adjustment ───────────────────────────────────────────────────

class TestRegimeAdjustment:
    def test_none_regime(self):
        assert _regime_adjustment(make_signal(), None) == 0

    def test_mean_reversion_penalized_in_strong_trend(self):
        regime = RegimeSnapshot(ts=0, trend="trending_up", trend_strength=50)
        adj = _regime_adjustment(make_signal(strategy="mean_reversion"), regime)
        assert adj == -8

    def test_momentum_boosted_in_trend(self):
        regime = RegimeSnapshot(ts=0, trend="trending_up", trend_strength=40)
        adj = _regime_adjustment(make_signal(side="long", strategy="momentum_swing"), regime)
        assert adj == 5

    def test_momentum_penalized_in_ranging(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", trend_strength=10)
        adj = _regime_adjustment(make_signal(side="long", strategy="momentum_swing"), regime)
        assert adj == -4

    def test_squeeze_boosts_momentum(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", trend_strength=10, bb_squeeze=True)
        adj = _regime_adjustment(make_signal(side="long", strategy="momentum_swing"), regime)
        # ranging -4, squeeze +4 = 0
        assert adj == 0

    def test_low_vol_penalty(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", volatility="low_vol", bb_squeeze=False)
        adj = _regime_adjustment(make_signal(strategy="momentum_swing"), regime)
        # ranging -4, low_vol -3 = -7
        assert adj == -7

    def test_high_vol_penalizes_scalp(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", volatility="high_vol")
        adj = _regime_adjustment(make_signal(strategy="momentum_scalp"), regime)
        # ranging -4, high_vol scalp -4 = -8
        assert adj == -8

    def test_high_vol_boosts_mean_reversion(self):
        regime = RegimeSnapshot(ts=0, trend="ranging", volatility="high_vol")
        adj = _regime_adjustment(make_signal(strategy="mean_reversion"), regime)
        # ranging: no penalty for MR, high_vol MR +3 = 3
        assert adj == 3


# ── _options_adjustment ──────────────────────────────────────────────────

class TestOptionsAdjustment:
    def test_none_options(self):
        assert _options_adjustment(make_signal(), None) == 0

    def test_high_put_call_penalizes_long(self):
        options = OptionsSentiment(symbol="ETH", put_call_ratio=1.5,
                                  total_put_oi=100, total_call_oi=50,
                                  implied_vol_avg=0.5, skew_25d=None)
        adj = _options_adjustment(make_signal(side="long"), options)
        assert adj == -4

    def test_low_put_call_boosts_long(self):
        options = OptionsSentiment(symbol="ETH", put_call_ratio=0.5,
                                  total_put_oi=50, total_call_oi=100,
                                  implied_vol_avg=0.5, skew_25d=None)
        adj = _options_adjustment(make_signal(side="long"), options)
        assert adj == 4

    def test_negative_skew_penalizes_long(self):
        options = OptionsSentiment(symbol="ETH", put_call_ratio=1.0,
                                  total_put_oi=100, total_call_oi=100,
                                  implied_vol_avg=0.5, skew_25d=-15)
        adj = _options_adjustment(make_signal(side="long"), options)
        assert adj == -3


# ── _derivatives_adjustment ──────────────────────────────────────────────

class TestDerivativesAdjustment:
    def test_none_derivatives(self):
        assert _derivatives_adjustment(make_signal(), None) == 0

    def test_crowded_longs_penalize_long(self):
        deriv = DerivativesData(symbol="ETH", futures_basis_pct=0.8,
                                open_interest_usd=1e9, funding_rate=0.001,
                                mark_price=3000, index_price=2990)
        adj = _derivatives_adjustment(make_signal(side="long"), deriv)
        assert adj == -5

    def test_crowded_shorts_boost_long(self):
        deriv = DerivativesData(symbol="ETH", futures_basis_pct=-0.5,
                                open_interest_usd=1e9, funding_rate=-0.001,
                                mark_price=2990, index_price=3000)
        adj = _derivatives_adjustment(make_signal(side="long"), deriv)
        assert adj == 5


# ── _stablecoin_adjustment ───────────────────────────────────────────────

class TestStablecoinAdjustment:
    def test_none_stablecoin(self):
        assert _stablecoin_adjustment(make_signal(), None) == 0

    def test_capital_inflow_boosts_long(self):
        flows = StablecoinFlows(total_stablecoin_mcap=150e9,
                                mcap_change_24h_pct=0.5, mcap_change_7d_pct=1.0,
                                usdt_dominance=0.65, usdt_mcap=100e9, usdc_mcap=50e9)
        adj = _stablecoin_adjustment(make_signal(side="long"), flows)
        assert adj == 3

    def test_capital_outflow_penalizes_long(self):
        flows = StablecoinFlows(total_stablecoin_mcap=150e9,
                                mcap_change_24h_pct=-0.5, mcap_change_7d_pct=-1.0,
                                usdt_dominance=0.65, usdt_mcap=100e9, usdc_mcap=50e9)
        adj = _stablecoin_adjustment(make_signal(side="long"), flows)
        assert adj == -3


# ── _unlock_risk_adjustment ──────────────────────────────────────────────

class TestUnlockRiskAdjustment:
    def test_no_risk(self):
        assert _unlock_risk_adjustment(make_signal(), False) == 0

    def test_risk_penalizes_long(self):
        assert _unlock_risk_adjustment(make_signal(side="long"), True) == -8

    def test_risk_boosts_short(self):
        assert _unlock_risk_adjustment(make_signal(side="short"), True) == 3
