"""Configuration — environment variables, default scanner config, and parameter bounds."""

import os
from dotenv import load_dotenv
from src.types import ScannerConfig

load_dotenv()


def _optional(key: str) -> str | None:
    return os.environ.get(key) or None


def _num(key: str, fallback: float) -> float:
    v = os.environ.get(key)
    if not v:
        return fallback
    return float(v)


def _bool(key: str, fallback: bool) -> bool:
    v = os.environ.get(key)
    if not v:
        return fallback
    return v.lower() in ("true", "1")


class _Env:
    paper_trading = _bool("PAPER_TRADING", True)

    coinbase_api_key = _optional("COINBASE_API_KEY")
    coinbase_api_secret = _optional("COINBASE_API_SECRET")
    binance_api_key = _optional("BINANCE_API_KEY")
    binance_api_secret = _optional("BINANCE_API_SECRET")

    anthropic_api_key = _optional("ANTHROPIC_API_KEY")
    lunarcrush_api_key = _optional("LUNARCRUSH_API_KEY")
    cryptopanic_token = _optional("CRYPTOPANIC_TOKEN")
    whale_alert_api_key = _optional("WHALE_ALERT_API_KEY")

    max_position_usd = _num("MAX_POSITION_USD", 100)
    max_daily_loss_usd = _num("MAX_DAILY_LOSS_USD", 300)
    max_open_positions = int(_num("MAX_OPEN_POSITIONS", 5))

    log_analysis_interval_mins = int(_num("LOG_ANALYSIS_INTERVAL_MINS", 60))
    min_trades_for_analysis = int(_num("MIN_TRADES_FOR_ANALYSIS", 10))


env = _Env()

default_scanner_config = ScannerConfig()

CONFIG_BOUNDS: dict[str, tuple[float, float]] = {
    "momentum_pct_swing":            (0.01, 0.15),
    "momentum_pct_scalp":            (0.015, 0.10),
    "volume_multiplier_swing":       (1.5, 5.0),
    "volume_multiplier_scalp":       (1.5, 5.0),
    "lookback_ms_swing":             (1_800_000, 14_400_000),
    "lookback_ms_scalp":             (60_000, 600_000),
    "cooldown_ms_swing":             (3_600_000, 86_400_000),
    "cooldown_ms_scalp":             (300_000, 7_200_000),
    "vwap_deviation_pct":            (0.01, 0.10),
    "rsi_oversold":                  (20, 40),
    "rsi_overbought":                (60, 80),
    "min_qual_score_swing":          (45, 85),
    "min_qual_score_scalp":          (35, 75),
    "base_trail_pct_swing":          (0.04, 0.18),
    "base_trail_pct_scalp":          (0.02, 0.08),
    "max_trail_pct":                 (0.10, 0.35),
    "max_hold_ms_swing":             (14_400_000, 172_800_000),
    "max_hold_ms_scalp":             (1_800_000, 14_400_000),
    "funding_rate_extreme_threshold": (0.0005, 0.005),
    "narrative_velocity_threshold":  (1.5, 8.0),
    "max_watchlist":                 (10, 200),
}
