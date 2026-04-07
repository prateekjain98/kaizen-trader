"""All type definitions for the self-healing crypto trader."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["long", "short"]
Tier = Literal["scalp", "swing", "position"]
MarketPhase = Literal["bull", "bear", "neutral", "extreme_fear", "extreme_greed"]
ExitReason = Literal["trailing_stop", "take_profit", "partial_take_profit", "time_limit", "circuit_breaker", "manual", "error"]

StrategyId = Literal[
    "momentum_swing", "momentum_scalp", "listing_pump", "whale_accumulation",
    "token_unlock_short", "mean_reversion", "funding_extreme", "liquidation_cascade",
    "orderbook_imbalance", "narrative_momentum", "correlation_break", "smart_money_follow",
    "protocol_revenue", "fear_greed_contrarian", "cross_exchange_divergence",
]

SignalSource = Literal[
    "news", "social", "whale_alert", "on_chain", "listing_detector", "funding_rates",
    "orderbook", "fear_greed", "protocol_revenue", "price_action", "correlation", "liquidation_data",
]

LossReason = Literal[
    "entered_pump_top", "stop_too_tight", "stop_too_wide", "low_qual_score",
    "adverse_news", "wrong_market_phase", "correlation_failure", "funding_squeeze",
    "liquidation_cascade_against", "repeated_symbol_loss", "unknown",
]

LogLevel = Literal["info", "signal", "trade", "heal", "error", "warn"]


@dataclass
class TradeSignal:
    id: str
    symbol: str
    product_id: str
    strategy: str
    side: str
    tier: str
    score: float
    confidence: str
    sources: list[str]
    reasoning: str
    entry_price: float
    expires_at: float
    created_at: float
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    suggested_size_usd: Optional[float] = None


@dataclass
class Position:
    id: str
    symbol: str
    product_id: str
    strategy: str
    side: str
    tier: str
    entry_price: float
    quantity: float
    size_usd: float
    opened_at: float
    high_watermark: float
    low_watermark: float
    current_price: float
    trail_pct: float
    stop_price: float
    max_hold_ms: float
    qual_score: float
    signal_id: str
    status: str = "open"
    exit_price: Optional[float] = None
    closed_at: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: Optional[str] = None
    paper_trading: bool = True
    # Partial take-profit tracking
    partial_exit_pct: float = 0.0        # fraction already sold (0.0 to 1.0)
    original_quantity: Optional[float] = None  # quantity at open (before partial exits)
    # MAE/MFE tracking (Maximum Adverse/Favorable Excursion as % from entry)
    mae_pct: float = 0.0  # worst drawdown from entry (always <= 0 for longs)
    mfe_pct: float = 0.0  # best excursion from entry (always >= 0 for longs)


@dataclass
class Trade:
    id: str
    position_id: str
    side: str
    symbol: str
    quantity: float
    size_usd: float
    price: float
    status: str
    paper_trading: bool
    placed_at: float
    order_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class TradeDiagnosis:
    position_id: str
    symbol: str
    strategy: str
    pnl_pct: float
    hold_ms: float
    exit_reason: str
    loss_reason: str
    entry_qual_score: float
    market_phase_at_entry: str
    action: str
    parameter_changes: dict
    timestamp: float


@dataclass
class ScannerConfig:
    momentum_pct_swing: float = 0.02
    momentum_pct_scalp: float = 0.025
    volume_multiplier_swing: float = 2.0
    volume_multiplier_scalp: float = 2.5
    lookback_ms_swing: float = 3_600_000
    lookback_ms_scalp: float = 300_000
    cooldown_ms_swing: float = 43_200_000
    cooldown_ms_scalp: float = 1_200_000
    vwap_deviation_pct: float = 0.03
    rsi_oversold: float = 30
    rsi_overbought: float = 70
    min_qual_score_swing: float = 55
    min_qual_score_scalp: float = 45
    base_trail_pct_swing: float = 0.07
    base_trail_pct_scalp: float = 0.04
    max_trail_pct: float = 0.20
    max_hold_ms_swing: float = 43_200_000
    max_hold_ms_scalp: float = 7_200_000
    funding_rate_extreme_threshold: float = 0.001
    narrative_velocity_threshold: float = 3.0
    max_watchlist: float = 50


@dataclass
class MarketContext:
    phase: str
    btc_dominance: float
    fear_greed_index: float
    total_market_cap_change_d1: float
    timestamp: float


@dataclass
class LogEntry:
    id: str
    level: str
    message: str
    ts: float
    symbol: Optional[str] = None
    strategy: Optional[str] = None
    data: Optional[dict] = None
