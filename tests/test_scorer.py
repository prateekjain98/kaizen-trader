"""Tests for the multi-signal qualification scorer."""

import pytest

from src.qualification.scorer import (
    _clamp, _news_adjustment, _social_adjustment,
    _context_adjustment, _fear_greed_adjustment, qualify,
)
from src.signals.news import NewsSentiment
from src.signals.social import SocialSentiment
from src.types import ScannerConfig, MarketContext
from tests.conftest import make_signal


# ── _clamp ─────────────────────────────────────────────────────────────────

class TestClamp:
    def test_within_range(self):
        assert _clamp(5, 0, 10) == 5

    def test_below_min(self):
        assert _clamp(-5, 0, 10) == 0

    def test_above_max(self):
        assert _clamp(15, 0, 10) == 10

    def test_at_boundaries(self):
        assert _clamp(0, 0, 10) == 0
        assert _clamp(10, 0, 10) == 10


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
