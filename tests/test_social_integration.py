"""Tests for expanded LunarCrush social integration."""

import time
from unittest.mock import patch, MagicMock

import pytest

from src.signals.social import (
    SocialSentiment, fetch_topic_sentiment, fetch_social_time_series,
    _can_call_topic, _record_topic_call,
    _topic_cache, _topic_cache_at, _TOPIC_CACHE_TTL_MS,
)
from src.qualification.scorer import _social_adjustment
from tests.conftest import make_signal

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_social(**overrides) -> SocialSentiment:
    defaults = dict(
        symbol="ETH", galaxy_score=50, alt_rank=100,
        social_volume=500, velocity_multiple=1.0,
        sentiment=0.0, sampled_at=time.time() * 1000,
        positive_pct=0.0, negative_pct=0.0, neutral_pct=0.0,
        social_volume_24h_change=0.0, alt_rank_change_24h=0,
    )
    defaults.update(overrides)
    return SocialSentiment(**defaults)


def _mock_topic_response(data: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": data}
    return mock_resp


def _mock_time_series_response(data: list) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": data}
    return mock_resp


# ── SocialSentiment new fields ────────────────────────────────────────────

class TestSocialSentimentFields:
    def test_default_new_fields(self):
        s = SocialSentiment(
            symbol="BTC", galaxy_score=60, alt_rank=5,
            social_volume=1000, velocity_multiple=2.0,
            sentiment=0.3, sampled_at=0,
        )
        assert s.positive_pct == 0.0
        assert s.negative_pct == 0.0
        assert s.neutral_pct == 0.0
        assert s.social_volume_24h_change == 0.0
        assert s.alt_rank_change_24h == 0

    def test_new_fields_populated(self):
        s = SocialSentiment(
            symbol="BTC", galaxy_score=60, alt_rank=5,
            social_volume=1000, velocity_multiple=2.0,
            sentiment=0.3, sampled_at=0,
            positive_pct=55.0, negative_pct=20.0, neutral_pct=25.0,
            social_volume_24h_change=120.0, alt_rank_change_24h=-15,
        )
        assert s.positive_pct == 55.0
        assert s.negative_pct == 20.0
        assert s.neutral_pct == 25.0
        assert s.social_volume_24h_change == 120.0
        assert s.alt_rank_change_24h == -15


# ── Rate limit tracking ──────────────────────────────────────────────────

class TestRateLimiting:
    def test_can_call_topic_initially(self):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 0
        mod._topic_minute_start = 0
        assert _can_call_topic() is True

    def test_can_call_topic_at_limit(self):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 3
        mod._topic_minute_start = time.time()
        assert _can_call_topic() is False

    def test_can_call_topic_resets_after_minute(self):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 3
        mod._topic_minute_start = time.time() - 61  # Over a minute ago
        assert _can_call_topic() is True

    def test_record_topic_call_increments(self):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 0
        mod._topic_minute_start = time.time()
        _record_topic_call()
        assert mod._topic_requests_this_minute == 1
        _record_topic_call()
        assert mod._topic_requests_this_minute == 2


# ── fetch_topic_sentiment ────────────────────────────────────────────────

class TestFetchTopicSentiment:
    def test_returns_none_without_api_key(self):
        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = None
            result = fetch_topic_sentiment("ETH")
            assert result is None

    @patch("src.signals.social.requests.get")
    def test_fetches_and_parses_topic(self, mock_get):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 0
        mod._topic_minute_start = time.time()
        # Clear cache
        mod._topic_cache.clear()
        mod._topic_cache_at.clear()

        mock_get.return_value = _mock_topic_response({
            "galaxy_score": 75,
            "alt_rank": 8,
            "social_volume": 2000,
            "sentiment_positive_pct": 65.0,
            "sentiment_negative_pct": 15.0,
            "sentiment_neutral_pct": 20.0,
            "social_volume_24h_change": 50.0,
            "alt_rank_change_24h": -10,
        })

        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = "test-key"
            with patch("src.signals.social._breaker") as mock_breaker:
                mock_breaker.can_call.return_value = True
                result = fetch_topic_sentiment("ETH")

        assert result is not None
        assert result.symbol == "ETH"
        assert result.galaxy_score == 75
        assert result.alt_rank == 8
        assert result.positive_pct == 65.0
        assert result.negative_pct == 15.0
        assert result.social_volume_24h_change == 50.0
        assert result.alt_rank_change_24h == -10

    def test_returns_cached_topic_within_ttl(self):
        import src.signals.social as mod
        now = time.time() * 1000
        cached = _make_social(symbol="SOL", galaxy_score=80)
        mod._topic_cache["SOL"] = cached
        mod._topic_cache_at["SOL"] = now

        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = "test-key"
            result = fetch_topic_sentiment("SOL")

        assert result is cached

    @patch("src.signals.social.requests.get")
    def test_handles_api_error(self, mock_get):
        import src.signals.social as mod
        mod._topic_requests_this_minute = 0
        mod._topic_minute_start = time.time()
        mod._topic_cache.clear()
        mod._topic_cache_at.clear()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = "test-key"
            with patch("src.signals.social._breaker") as mock_breaker:
                mock_breaker.can_call.return_value = True
                result = fetch_topic_sentiment("ETH")

        assert result is None  # No cache available


# ── fetch_social_time_series ─────────────────────────────────────────────

class TestFetchSocialTimeSeries:
    def test_returns_empty_without_api_key(self):
        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = None
            result = fetch_social_time_series("ETH")
            assert result == []

    @patch("src.signals.social.requests.get")
    def test_fetches_time_series(self, mock_get):
        series_data = [
            {"ts": 1000, "social_volume": 500, "galaxy_score": 60},
            {"ts": 2000, "social_volume": 800, "galaxy_score": 65},
        ]
        mock_get.return_value = _mock_time_series_response(series_data)

        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = "test-key"
            with patch("src.signals.social._breaker") as mock_breaker:
                mock_breaker.can_call.return_value = True
                result = fetch_social_time_series("ETH", interval="1h", data_points=2)

        assert len(result) == 2
        assert result[0]["social_volume"] == 500
        assert result[1]["galaxy_score"] == 65

    @patch("src.signals.social.requests.get")
    def test_time_series_network_error(self, mock_get):
        mock_get.side_effect = Exception("timeout")

        with patch("src.signals.social.env") as mock_env:
            mock_env.lunarcrush_api_key = "test-key"
            with patch("src.signals.social._breaker") as mock_breaker:
                mock_breaker.can_call.return_value = True
                result = fetch_social_time_series("ETH")

        assert result == []


# ── Enhanced _social_adjustment ──────────────────────────────────────────

class TestEnhancedSocialAdjustment:
    def test_none_social(self):
        assert _social_adjustment(make_signal(), None) == 0

    def test_high_galaxy_score_boost(self):
        social = _make_social(galaxy_score=75)
        adj = _social_adjustment(make_signal(side="long"), social)
        # galaxy >= 70 -> +5
        assert adj == 5

    def test_low_galaxy_score_penalty(self):
        social = _make_social(galaxy_score=25)
        adj = _social_adjustment(make_signal(side="long"), social)
        # galaxy <= 30 -> -5
        assert adj == -5

    def test_high_velocity_boost(self):
        social = _make_social(velocity_multiple=3.5)
        adj = _social_adjustment(make_signal(side="long"), social)
        # velocity >= 3 -> +7
        assert adj == 7

    def test_medium_velocity_boost(self):
        social = _make_social(velocity_multiple=2.5)
        adj = _social_adjustment(make_signal(side="long"), social)
        # velocity >= 2 -> +3
        assert adj == 3

    def test_negative_sentiment_penalizes_long(self):
        social = _make_social(negative_pct=80, galaxy_score=50)
        adj = _social_adjustment(make_signal(side="long"), social)
        # negative > 70 + long -> -5
        assert adj == -5

    def test_negative_sentiment_no_penalty_for_short(self):
        social = _make_social(negative_pct=80, galaxy_score=50)
        adj = _social_adjustment(make_signal(side="short"), social)
        # negative > 70 but short -> no penalty from sentiment
        assert adj == 0

    def test_positive_sentiment_with_volume_boost(self):
        social = _make_social(positive_pct=80, social_volume=1000)
        adj = _social_adjustment(make_signal(side="long"), social)
        # positive > 70 + volume > 0 -> +3
        assert adj == 3

    def test_altrank_improvement_boost(self):
        social = _make_social(alt_rank_change_24h=-25)
        adj = _social_adjustment(make_signal(side="long"), social)
        # alt_rank_change < -20 -> +4
        assert adj == 4

    def test_altrank_decline_penalty(self):
        social = _make_social(alt_rank_change_24h=30)
        adj = _social_adjustment(make_signal(side="long"), social)
        # alt_rank_change > 20 -> -3
        assert adj == -3

    def test_social_volume_surge_boost(self):
        social = _make_social(social_volume_24h_change=150)
        adj = _social_adjustment(make_signal(side="long"), social)
        # volume change > 100 -> +3
        assert adj == 3

    def test_social_volume_drop_penalty(self):
        social = _make_social(social_volume_24h_change=-60)
        adj = _social_adjustment(make_signal(side="long"), social)
        # volume change < -50 -> -2
        assert adj == -2

    def test_combined_signals_clamped(self):
        social = _make_social(
            galaxy_score=80, velocity_multiple=4.0,
            positive_pct=85, social_volume=1000,
            alt_rank_change_24h=-30, social_volume_24h_change=200,
        )
        adj = _social_adjustment(make_signal(side="long"), social)
        # +5 (galaxy) +7 (velocity) +3 (positive) +4 (altrank) +3 (volume) = 22
        # Clamped to 12
        assert adj == 12

    def test_combined_negative_clamped(self):
        social = _make_social(
            galaxy_score=20, negative_pct=80,
            alt_rank_change_24h=30, social_volume_24h_change=-60,
        )
        adj = _social_adjustment(make_signal(side="long"), social)
        # -5 (galaxy) -5 (negative+long) -3 (altrank) -2 (volume) = -15
        # Clamped to -12
        assert adj == -12
