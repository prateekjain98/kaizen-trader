#!/usr/bin/env python3
"""Systematic Strategy Backtester — tests each strategy one-by-one,
analyzes every losing trade, flags issues, and updates parameters continuously.

Usage:
    python3 scripts/systematic_backtest.py
    python3 scripts/systematic_backtest.py --symbols BTC,ETH,SOL --start 2025-01-01 --end 2025-03-31
    python3 scripts/systematic_backtest.py --strategy momentum_swing --verbose
"""

import argparse
import copy
import dataclasses
import datetime
import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.types import TradeSignal, Position, ScannerConfig, MarketContext
from src.config import CONFIG_BOUNDS, default_scanner_config
from src.backtesting.data_loader import load_klines, load_futures_klines
from src.backtesting.funding_loader import load_funding_rates
from src.backtesting.oi_loader import load_open_interest
from src.backtesting.listing_loader import load_exchange_listings, get_listing_events_in_range
from src.indicators.core import compute_rsi, compute_atr, compute_ema, compute_bollinger_bands, compute_adx, compute_macd, compute_obv, OHLCV

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SYMBOLS = [
    # Tier 1: Top 30 by market cap (all have data from 2020+)
    "BTC", "ETH", "BNB", "XRP", "ADA", "DOGE", "SOL", "DOT", "LINK",
    "LTC", "AVAX", "UNI", "ATOM", "ETC", "XLM", "TRX", "ALGO", "MATIC",
    "FIL", "AAVE", "EOS", "XTZ", "THETA", "VET", "NEO", "DASH", "ZEC",
    "COMP", "SNX", "MKR",
    # Tier 2: Mid-cap alts with 2020+ data
    "SUSHI", "YFI", "CRV", "BAL", "KNC", "REN", "BAND", "KAVA", "QTUM",
    "ZIL", "BAT", "ENJ", "HBAR", "ONE", "HOT", "CHZ", "IOST", "ONT",
    "ICX", "IOTA", "ZRX", "RVN", "STX", "FET", "ANKR", "CELR", "CHR",
    "COTI", "CTSI", "CVC", "DENT", "DUSK", "FUN", "IOTX", "MTL", "OGN",
    "RLC", "ARPA", "BNT", "LSK",
    # Tier 3: Newer listings with 2021+ data
    "NEAR", "FTM", "SAND", "MANA", "AXS", "GALA", "IMX", "LDO", "APE",
    "OP", "ARB", "SUI", "APT", "SEI", "INJ", "TIA", "BONK", "WIF",
    "PEPE", "FLOKI", "ORDI", "RENDER", "PENDLE", "JUP", "WLD", "STRK",
    "ONDO", "DYDX", "GMX", "GRT", "ROSE", "FLOW",
    # Tier 4: Additional small caps
    "COS", "ONG", "TFUEL", "WAN", "WIN", "HIVE", "MBL", "MDT", "ARDR",
]
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2025-03-31"
DEFAULT_BALANCE = 10_000.0
COMMISSION_PCT = 0.00075   # Binance with BNB discount (0.075% per side)
SLIPPAGE_PCT = 0.0005
FIXED_POSITION_SIZE = 100  # $100 per trade — no compounding, fair comparison
REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

# ---------------------------------------------------------------------------
# Loss classification categories
# ---------------------------------------------------------------------------

class LossCategory:
    DATA_INSUFFICIENT = "data_insufficient"
    ENTRY_ERROR = "entry_error"
    EXIT_ERROR = "exit_error"
    PARAMETER_ISSUE = "parameter_issue"
    MARKET_CONDITION = "market_condition"
    STRATEGY_LOGIC = "strategy_logic"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _date_to_ms(date_str: str) -> int:
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: float) -> str:
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _compute_momentum_pct(candles: list[dict], lookback: int) -> Optional[float]:
    if len(candles) < lookback:
        return None
    old = candles[-lookback]["close"]
    new = candles[-1]["close"]
    return (new - old) / old if old > 0 else None


def _compute_volume_ratio(candles: list[dict], lookback: int) -> float:
    if len(candles) < lookback or lookback == 0:
        return 0
    recent = candles[-lookback:]
    avg_vol = sum(c["volume"] for c in recent) / len(recent)
    return candles[-1]["volume"] / avg_vol if avg_vol > 0 else 0


def _compute_vwap(candles: list[dict]) -> Optional[float]:
    if not candles:
        return None
    sum_pv = sum(c["close"] * c["volume"] for c in candles)
    sum_v = sum(c["volume"] for c in candles)
    return sum_pv / sum_v if sum_v > 0 else None


def _compute_rsi_from_candles(candles: list[dict], period: int = 14) -> Optional[float]:
    closes = [c["close"] for c in candles]
    return compute_rsi(closes, period)


def _compute_atr_from_candles(candles: list[dict], period: int = 14) -> Optional[float]:
    ohlcvs = [OHLCV(c["open"], c["high"], c["low"], c["close"], c["volume"], c["open_time"]) for c in candles]
    return compute_atr(ohlcvs, period)


def _derive_market_phase(btc_candles: list[dict]) -> str:
    """Derive market phase from BTC RSI, matching production fear_greed_to_market_phase().

    Production maps FGI (Fear & Greed Index) to phases:
      <=20 -> extreme_fear, >=80 -> extreme_greed, <=40 -> bear, >=60 -> bull
    We approximate FGI from RSI (FGI ≈ RSI*0.8 + 10), so:
      RSI<=12.5 -> extreme_fear, RSI>=87.5 -> extreme_greed
      RSI<=37.5 -> bear, RSI>=62.5 -> bull
    """
    if len(btc_candles) < 30:
        return "neutral"
    closes = [c["close"] for c in btc_candles[-30:]]
    rsi = compute_rsi(closes)
    if rsi is None:
        return "neutral"
    # Approximate FGI from RSI
    fgi = rsi * 0.8 + 10
    if fgi <= 20:
        return "extreme_fear"
    if fgi >= 80:
        return "extreme_greed"
    if fgi <= 40:
        return "bear"
    if fgi >= 60:
        return "bull"
    return "neutral"


def _derive_fear_greed(btc_candles: list[dict]) -> float:
    """Approximate Fear & Greed index from BTC RSI + volatility."""
    if len(btc_candles) < 20:
        return 50
    closes = [c["close"] for c in btc_candles[-20:]]
    rsi = compute_rsi(closes)
    if rsi is None:
        return 50
    # Map RSI 0-100 to FGI 0-100 with some noise
    return max(0, min(100, rsi * 0.8 + 10))


# ---------------------------------------------------------------------------
# Position management (shared by all strategy simulators)
# ---------------------------------------------------------------------------

@dataclass
class BacktestPosition:
    """Lightweight position for backtest tracking."""
    id: str
    symbol: str
    strategy: str
    side: str
    tier: str
    entry_price: float
    size_usd: float
    quantity: float
    opened_at: float
    high_watermark: float
    low_watermark: float
    current_price: float
    trail_pct: float
    stop_price: float
    max_hold_ms: float
    qual_score: float
    signal_reasoning: str
    target_price: Optional[float] = None
    exit_price: Optional[float] = None
    closed_at: Optional[float] = None
    pnl_pct: Optional[float] = None
    pnl_usd: Optional[float] = None
    exit_reason: Optional[str] = None
    momentum_at_entry: float = 0.0
    # Context at entry for loss analysis
    rsi_at_entry: Optional[float] = None
    atr_at_entry: Optional[float] = None
    volume_ratio_at_entry: float = 0.0
    market_phase_at_entry: str = "neutral"
    candle_count_at_entry: int = 0
    mae_pct: float = 0.0  # max adverse excursion
    mfe_pct: float = 0.0  # max favorable excursion


@dataclass
class LossAnalysis:
    """Detailed analysis of a losing trade."""
    position_id: str
    symbol: str
    strategy: str
    category: str  # LossCategory
    loss_reason: str  # specific reason code
    explanation: str  # human-readable explanation
    pnl_pct: float
    pnl_usd: float
    hold_hours: float
    exit_reason: str
    market_phase: str
    entry_price: float
    exit_price: float
    signal_reasoning: str
    data_quality: dict  # indicators available, candle count, etc.
    suggested_fix: str
    parameter_change: Optional[dict] = None  # {"key": str, "old": float, "new": float}
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class StrategyBacktestResult:
    """Result of backtesting a single strategy."""
    strategy: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    max_drawdown_pct: float
    avg_hold_hours: float
    loss_analyses: list[LossAnalysis]
    parameter_changes: list[dict]
    issues_found: list[str]
    data_quality_score: float  # 0-100, how much data was available
    final_config: dict


# ---------------------------------------------------------------------------
# Loss Analyzer
# ---------------------------------------------------------------------------

class LossAnalyzer:
    """Deep analysis of every losing trade."""

    def analyze(self, pos: BacktestPosition, candles_at_close: list[dict],
                config: ScannerConfig) -> LossAnalysis:
        hold_ms = (pos.closed_at or 0) - pos.opened_at
        hold_hours = hold_ms / 3_600_000

        # Gather data quality info
        data_quality = {
            "candles_at_entry": pos.candle_count_at_entry,
            "rsi_available": pos.rsi_at_entry is not None,
            "atr_available": pos.atr_at_entry is not None,
            "volume_ratio": pos.volume_ratio_at_entry,
            "candles_at_close": len(candles_at_close),
        }

        # Classify the loss
        category, reason, explanation, fix = self._classify(
            pos, hold_hours, candles_at_close, config, data_quality
        )

        # Compute parameter change if applicable
        param_change = self._suggest_parameter_change(reason, pos, config)

        return LossAnalysis(
            position_id=pos.id,
            symbol=pos.symbol,
            strategy=pos.strategy,
            category=category,
            loss_reason=reason,
            explanation=explanation,
            pnl_pct=pos.pnl_pct or 0,
            pnl_usd=pos.pnl_usd or 0,
            hold_hours=hold_hours,
            exit_reason=pos.exit_reason or "unknown",
            market_phase=pos.market_phase_at_entry,
            entry_price=pos.entry_price,
            exit_price=pos.exit_price or 0,
            signal_reasoning=pos.signal_reasoning,
            data_quality=data_quality,
            suggested_fix=fix,
            parameter_change=param_change,
            timestamp=time.time() * 1000,
        )

    def _classify(self, pos: BacktestPosition, hold_hours: float,
                  candles: list[dict], config: ScannerConfig,
                  data_quality: dict) -> tuple[str, str, str, str]:
        """Returns (category, reason, explanation, suggested_fix)."""
        pnl_pct = pos.pnl_pct or 0

        # 1. DATA INSUFFICIENT — not enough candles or indicators
        if data_quality["candles_at_entry"] < 15:
            return (
                LossCategory.DATA_INSUFFICIENT,
                "insufficient_candles",
                f"Only {data_quality['candles_at_entry']} candles at entry (need 15+ for reliable indicators). "
                f"RSI/VWAP/ATR may have been unreliable.",
                "Require minimum 20 candles before generating signals for this strategy",
            )
        if not data_quality["rsi_available"] and pos.strategy in ("mean_reversion",):
            return (
                LossCategory.DATA_INSUFFICIENT,
                "rsi_unavailable",
                "RSI was not computable at entry — mean reversion signal was based on incomplete data.",
                "Gate mean_reversion signals on RSI availability (need 15+ candles)",
            )
        if not data_quality["atr_available"]:
            return (
                LossCategory.DATA_INSUFFICIENT,
                "atr_unavailable",
                "ATR was unavailable — stop distance used fixed percentage instead of volatility-adaptive stop.",
                "Use wider fixed stop when ATR unavailable, or skip signal entirely",
            )

        # 2. ENTRY ERROR — entered at a bad time
        if pos.momentum_at_entry > 0.08 and hold_hours < 4:
            return (
                LossCategory.ENTRY_ERROR,
                "entered_pump_top",
                f"Entered with {pos.momentum_at_entry*100:.1f}% momentum — likely chased a pump top. "
                f"Price reversed within {hold_hours:.1f}h.",
                "Raise momentum threshold to filter out late entries",
            )
        if pos.market_phase_at_entry in ("extreme_fear",) and pos.side == "long" and pos.strategy != "fear_greed_contrarian":
            return (
                LossCategory.ENTRY_ERROR,
                "wrong_market_phase",
                f"Opened LONG during extreme_fear phase — most momentum/breakout longs fail here.",
                "Add market phase filter: skip long signals during extreme_fear for non-contrarian strategies",
            )
        if pos.market_phase_at_entry in ("bear",) and pos.side == "long" and pos.strategy == "momentum_swing":
            return (
                LossCategory.ENTRY_ERROR,
                "momentum_in_bear_market",
                f"Momentum long in bear market — breakouts tend to be bull traps in downtrends.",
                "Reduce momentum position size or increase threshold in bear phase",
            )

        # 3. EXIT ERROR — stop/timing issues
        if hold_hours < 2 and pos.exit_reason == "trailing_stop":
            return (
                LossCategory.EXIT_ERROR,
                "stop_too_tight",
                f"Trailing stop hit in {hold_hours:.1f}h — stop was {pos.trail_pct*100:.1f}% from entry. "
                f"MAE was {pos.mae_pct*100:.2f}%, MFE was {pos.mfe_pct*100:.2f}%.",
                "Widen trailing stop or use ATR-based stops for this strategy/tier",
            )
        if hold_hours > 20 and pnl_pct < -0.05:
            return (
                LossCategory.EXIT_ERROR,
                "stop_too_wide",
                f"Held for {hold_hours:.1f}h with {pnl_pct*100:.1f}% loss — stop was too far from price action.",
                "Tighten trailing stop or reduce max hold time",
            )
        if pos.exit_reason == "time_limit" and pos.mfe_pct > 0.03:
            return (
                LossCategory.EXIT_ERROR,
                "missed_take_profit",
                f"Time limit hit but MFE was +{pos.mfe_pct*100:.1f}% — trade was profitable but reversed. "
                f"No take-profit was set or it was too ambitious.",
                "Add partial take-profit at 50% of MFE to lock in gains before time limit",
            )

        # 4. PARAMETER ISSUE — thresholds
        if pos.qual_score < (config.min_qual_score_scalp if pos.tier == "scalp" else config.min_qual_score_swing):
            return (
                LossCategory.PARAMETER_ISSUE,
                "low_qual_score",
                f"Signal quality score {pos.qual_score:.0f} was below threshold — trade shouldn't have been taken.",
                "Raise min_qual_score to filter weak signals",
            )
        if pos.strategy == "funding_extreme":
            return (
                LossCategory.PARAMETER_ISSUE,
                "funding_squeeze",
                f"Funding extreme trade lost — rate may not have been extreme enough to trigger reversal.",
                "Lower funding_rate_extreme_threshold to require stronger extremes",
            )

        # 5. MARKET CONDITION — regime issues
        if pos.market_phase_at_entry == "neutral" and abs(pnl_pct) < 0.03:
            return (
                LossCategory.MARKET_CONDITION,
                "low_volatility_chop",
                f"Small loss ({pnl_pct*100:.1f}%) in neutral market — price action was choppy with no clear direction.",
                "Consider reducing position size or widening stops in neutral/low-volatility regimes",
            )

        # 6. STRATEGY LOGIC — catch-all for potential bugs
        if pos.mfe_pct > abs(pnl_pct) * 2:
            return (
                LossCategory.STRATEGY_LOGIC,
                "profit_reversal",
                f"Trade reached +{pos.mfe_pct*100:.1f}% MFE but closed at {pnl_pct*100:.1f}% — "
                f"profitable trade turned into a loss. Trailing stop didn't lock gains.",
                "Review trail_pct tightening logic — trail may be too wide for this strategy",
            )

        return (
            LossCategory.MARKET_CONDITION,
            "adverse_move",
            f"Price moved {pnl_pct*100:.1f}% against position. Hold: {hold_hours:.1f}h, "
            f"exit: {pos.exit_reason}.",
            "No clear fix — normal market risk",
        )

    def _suggest_parameter_change(self, reason: str, pos: BacktestPosition,
                                  config: ScannerConfig) -> Optional[dict]:
        tier = pos.tier
        if reason == "entered_pump_top":
            key = "momentum_pct_swing" if tier == "swing" else "momentum_pct_scalp"
            old = getattr(config, key)
            new = min(CONFIG_BOUNDS[key][1], old + 0.01)
            if new != old:
                return {"key": key, "old": old, "new": new}
        elif reason == "stop_too_tight":
            key = "base_trail_pct_swing" if tier == "swing" else "base_trail_pct_scalp"
            old = getattr(config, key)
            new = min(CONFIG_BOUNDS[key][1], old + 0.01)
            if new != old:
                return {"key": key, "old": old, "new": new}
        elif reason == "stop_too_wide":
            key = "base_trail_pct_swing" if tier == "swing" else "base_trail_pct_scalp"
            old = getattr(config, key)
            new = max(CONFIG_BOUNDS[key][0], old - 0.01)
            if new != old:
                return {"key": key, "old": old, "new": new}
        elif reason == "low_qual_score":
            key = "min_qual_score_swing" if tier == "swing" else "min_qual_score_scalp"
            old = getattr(config, key)
            new = min(CONFIG_BOUNDS[key][1], old + 2)
            if new != old:
                return {"key": key, "old": old, "new": new}
        elif reason == "funding_squeeze":
            key = "funding_rate_extreme_threshold"
            old = getattr(config, key)
            # RAISE threshold to require stronger extremes (not lower!)
            new = min(CONFIG_BOUNDS[key][1], old + 0.0002)
            if new != old:
                return {"key": key, "old": old, "new": new}
        return None


# ---------------------------------------------------------------------------
# Strategy Simulator — base class
# ---------------------------------------------------------------------------

class StrategySimulator:
    """Base class for strategy backtest simulators.

    Each strategy subclass implements:
    - scan(symbol, candles, config, market_ctx, now_ms) -> Optional[TradeSignal]
    - required_candles() -> int  (minimum candle window)
    - timeframe() -> str  (candle interval: "1h", "4h", "1d", "5m")
    - data_feeds() -> list[str]  (what external data it needs)
    """
    strategy_id: str = ""
    tier: str = "swing"

    def __init__(self):
        self.balance = DEFAULT_BALANCE
        self.open_positions: list[BacktestPosition] = []
        self.closed_positions: list[BacktestPosition] = []
        self.cooldowns: dict[str, float] = {}
        self.analyzer = LossAnalyzer()
        self.loss_analyses: list[LossAnalysis] = []
        self.parameter_changes: list[dict] = []
        self.issues: list[str] = []
        self.peak_equity = DEFAULT_BALANCE
        self.max_dd = 0.0

    def required_candles(self) -> int:
        return 30

    def timeframe(self) -> str:
        """Candle interval this strategy needs. Subclasses override."""
        return "1h"

    def data_feeds(self) -> list[str]:
        return ["klines"]

    def scan(self, symbol: str, candles: list[dict], config: ScannerConfig,
             ctx: MarketContext, now_ms: float) -> Optional[TradeSignal]:
        raise NotImplementedError

    def _apply_slippage(self, price: float, side: str, entry: bool) -> float:
        if (side == "long" and entry) or (side == "short" and not entry):
            return price * (1 + SLIPPAGE_PCT)
        return price * (1 - SLIPPAGE_PCT)

    def _position_size(self, qual_score: float) -> float:
        """Kelly compounding with realistic $50K cap.
        LOCKED — do not change. This is the definitive sizing."""
        if self.balance < 50:
            return 0
        fraction = 0.25
        qual_mult = 0.5 + (qual_score / 100)
        raw = fraction * self.balance * qual_mult
        max_balance_pct = self.balance * 0.40
        MAX_POSITION = 50_000  # realistic exchange limit
        return max(10, min(raw, max_balance_pct, MAX_POSITION))

    def _check_cooldown(self, symbol: str, now_ms: float) -> bool:
        return now_ms < self.cooldowns.get(symbol, 0)

    def _set_cooldown(self, symbol: str, now_ms: float, duration_ms: float):
        self.cooldowns[symbol] = now_ms + duration_ms

    def open_position(self, signal: TradeSignal, now_ms: float, config: ScannerConfig,
                      candles: list[dict], ctx: MarketContext) -> Optional[BacktestPosition]:
        if len(self.open_positions) >= 20:  # max 20 open per strategy
            return None
        if any(p.symbol == signal.symbol for p in self.open_positions):
            return None

        size_usd = self._position_size(signal.score)
        entry_price = self._apply_slippage(signal.entry_price, signal.side, True)
        commission = size_usd * COMMISSION_PCT

        if size_usd + commission > self.balance:
            return None

        self.balance -= (size_usd + commission)  # Reserve capital for position
        quantity = size_usd / entry_price

        default_trail = (config.base_trail_pct_scalp if signal.tier == "scalp"
                         else config.base_trail_pct_swing)
        max_hold = (config.max_hold_ms_scalp if signal.tier == "scalp"
                    else config.max_hold_ms_swing)

        # Use signal's explicit stop_price if set — derive trail_pct from it
        # so the trailing stop matches the strategy's intended risk level.
        if signal.stop_price and signal.stop_price > 0:
            stop_price = signal.stop_price
            if signal.side == "long":
                trail_pct = max(0.005, (entry_price - stop_price) / entry_price)
            else:
                trail_pct = max(0.005, (stop_price - entry_price) / entry_price)
        else:
            trail_pct = default_trail
            stop_price = (entry_price * (1 - trail_pct) if signal.side == "long"
                          else entry_price * (1 + trail_pct))

        # Compute entry context for loss analysis
        momentum = _compute_momentum_pct(candles, min(len(candles), 24))
        rsi = _compute_rsi_from_candles(candles)
        atr = _compute_atr_from_candles(candles)
        vol_ratio = _compute_volume_ratio(candles, min(len(candles), 20))

        pos = BacktestPosition(
            id=str(uuid.uuid4()), symbol=signal.symbol, strategy=signal.strategy,
            side=signal.side, tier=signal.tier, entry_price=entry_price,
            size_usd=size_usd, quantity=quantity, opened_at=now_ms,
            high_watermark=entry_price, low_watermark=entry_price,
            current_price=entry_price, trail_pct=trail_pct,
            stop_price=stop_price, max_hold_ms=max_hold, qual_score=signal.score,
            signal_reasoning=signal.reasoning,
            target_price=signal.target_price,
            momentum_at_entry=momentum or 0.0,
            rsi_at_entry=rsi, atr_at_entry=atr,
            volume_ratio_at_entry=vol_ratio,
            market_phase_at_entry=ctx.phase,
            candle_count_at_entry=len(candles),
        )
        self.open_positions.append(pos)
        return pos

    def update_positions(self, candle: dict, now_ms: float, candles: list[dict],
                         config: ScannerConfig, verbose: bool = False) -> list[BacktestPosition]:
        """Update open positions with candle data. Returns list of closed positions."""
        high, low, close = candle["high"], candle["low"], candle["close"]
        closed_this_tick: list[BacktestPosition] = []
        still_open: list[BacktestPosition] = []

        for pos in self.open_positions:
            if pos.symbol != candle.get("_symbol", pos.symbol):
                still_open.append(pos)
                continue

            # Update watermarks and MAE/MFE
            pos.high_watermark = max(pos.high_watermark, high)
            pos.low_watermark = min(pos.low_watermark, low)
            pos.current_price = close

            if pos.side == "long":
                excursion = (close - pos.entry_price) / pos.entry_price
                pos.mfe_pct = max(pos.mfe_pct, (pos.high_watermark - pos.entry_price) / pos.entry_price)
                pos.mae_pct = min(pos.mae_pct, (pos.low_watermark - pos.entry_price) / pos.entry_price)
            else:
                excursion = (pos.entry_price - close) / pos.entry_price
                pos.mfe_pct = max(pos.mfe_pct, (pos.entry_price - pos.low_watermark) / pos.entry_price)
                pos.mae_pct = min(pos.mae_pct, (pos.entry_price - pos.high_watermark) / pos.entry_price)

            # Update trailing stop — but ONLY after position is in meaningful profit.
            # This prevents trailing stops from cutting winners too early.
            # Before the profit threshold, the original fixed stop stays in place.
            min_profit_to_trail = pos.trail_pct * 1.3  # need 130% of stop distance (3.9% for 3% trail) before trailing
            if pos.side == "long":
                current_profit = (pos.high_watermark - pos.entry_price) / pos.entry_price
                if current_profit >= min_profit_to_trail:
                    new_stop = pos.high_watermark * (1 - pos.trail_pct)
                    if new_stop > pos.stop_price:
                        pos.stop_price = new_stop
            else:
                current_profit = (pos.entry_price - pos.low_watermark) / pos.entry_price
                if current_profit >= min_profit_to_trail:
                    new_stop = pos.low_watermark * (1 + pos.trail_pct)
                    if new_stop < pos.stop_price:
                        pos.stop_price = new_stop

            # Check exits — take_profit FIRST (within same candle, TP often
            # triggers before trailing stop when price spikes through both)
            exit_reason = None
            exit_price = close

            if pos.target_price:
                if pos.side == "long" and high >= pos.target_price:
                    exit_reason = "take_profit"
                    exit_price = pos.target_price
                elif pos.side == "short" and low <= pos.target_price:
                    exit_reason = "take_profit"
                    exit_price = pos.target_price

            if exit_reason is None:
                if pos.side == "long" and low <= pos.stop_price:
                    exit_reason = "trailing_stop"
                    exit_price = pos.stop_price
                elif pos.side == "short" and high >= pos.stop_price:
                    exit_reason = "trailing_stop"
                    exit_price = pos.stop_price

            if exit_reason is None and (now_ms - pos.opened_at) >= pos.max_hold_ms:
                exit_reason = "time_limit"
                exit_price = close

            if exit_reason:
                exit_price = self._apply_slippage(exit_price, pos.side, False)
                commission = pos.size_usd * COMMISSION_PCT

                if pos.side == "long":
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
                else:
                    pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

                pnl_usd = pos.size_usd * pnl_pct - commission
                pos.exit_price = exit_price
                pos.closed_at = now_ms
                pos.pnl_pct = pnl_pct
                pos.pnl_usd = pnl_usd
                pos.exit_reason = exit_reason

                self.balance += pos.size_usd + pnl_usd
                self.closed_positions.append(pos)
                closed_this_tick.append(pos)

                # LOSS ANALYSIS — the core of this backtester
                is_loss = pnl_pct < -0.005
                if is_loss:
                    analysis = self.analyzer.analyze(pos, candles, config)
                    self.loss_analyses.append(analysis)

                    if verbose:
                        print(f"  LOSS {pos.symbol} {pos.strategy} {pnl_pct*100:.2f}% "
                              f"[{analysis.category}:{analysis.loss_reason}] {analysis.explanation[:80]}")

                    # Self-healing DISABLED during backtest — test strategies with intended params
                    # (keeping analysis for reporting but NOT mutating config)
                    if analysis.parameter_change:
                        pc = analysis.parameter_change
                        self.parameter_changes.append({
                            "trade_id": pos.id,
                            "symbol": pos.symbol,
                            "reason": analysis.loss_reason,
                            "key": pc["key"],
                            "old": pc["old"],
                            "new": pc.get("new", pc["old"]),
                        })
                elif verbose:
                    print(f"  WIN  {pos.symbol} {pos.strategy} +{pnl_pct*100:.2f}%")
            else:
                still_open.append(pos)

        self.open_positions = still_open
        return closed_this_tick

    def force_close_all(self, candle_windows: dict[str, list[dict]], now_ms: float):
        for pos in list(self.open_positions):
            last_candles = candle_windows.get(pos.symbol, [])
            last_price = last_candles[-1]["close"] if last_candles else pos.current_price
            exit_price = self._apply_slippage(last_price, pos.side, False)
            commission = pos.size_usd * COMMISSION_PCT

            if pos.side == "long":
                pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

            pos.exit_price = exit_price
            pos.closed_at = now_ms
            pos.pnl_pct = pnl_pct
            pos.pnl_usd = pos.size_usd * pnl_pct - commission
            pos.exit_reason = "time_limit"
            self.balance += pos.size_usd + pos.pnl_usd
            self.closed_positions.append(pos)
        self.open_positions = []

    def get_result(self, config: ScannerConfig) -> StrategyBacktestResult:
        trades = self.closed_positions
        wins = [t for t in trades if (t.pnl_pct or 0) > 0]
        losses = [t for t in trades if (t.pnl_pct or 0) <= 0]
        total_pnl = sum(t.pnl_usd or 0 for t in trades)
        hold_hours = [(t.closed_at - t.opened_at) / 3_600_000 for t in trades if t.closed_at]

        # Tally loss categories
        cat_counts: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        for la in self.loss_analyses:
            cat_counts[la.category] = cat_counts.get(la.category, 0) + 1
            reason_counts[la.loss_reason] = reason_counts.get(la.loss_reason, 0) + 1

        # Flag top issues
        for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
            if count >= 2:
                self.issues.append(f"{reason}: occurred {count} times")

        # Data quality score
        data_issues = sum(1 for la in self.loss_analyses
                          if la.category == LossCategory.DATA_INSUFFICIENT)
        total_losses = len(self.loss_analyses)
        data_quality = 100 - (data_issues / max(1, total_losses) * 100) if total_losses > 0 else 100

        return StrategyBacktestResult(
            strategy=self.strategy_id,
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0,
            total_pnl_usd=total_pnl,
            max_drawdown_pct=self.max_dd,
            avg_hold_hours=sum(hold_hours) / len(hold_hours) if hold_hours else 0,
            loss_analyses=self.loss_analyses,
            parameter_changes=self.parameter_changes,
            issues_found=self.issues,
            data_quality_score=data_quality,
            final_config=dataclasses.asdict(config),
        )


# ---------------------------------------------------------------------------
# Concrete strategy simulators
# ---------------------------------------------------------------------------

class MomentumSimulator(StrategySimulator):
    """Momentum PULLBACK strategy — buys pullbacks in confirmed uptrends.
    Research: pullback entry (RSI 40-50 in uptrend) beats breakout entry."""
    strategy_id = "momentum_swing"
    tier = "swing"

    def required_candles(self):
        return 60

    def timeframe(self):
        return "4h"

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 55:
            return None
        if ctx.phase in ("extreme_fear",):
            return None

        closes = [c["close"] for c in candles]
        price = candles[-1]["close"]

        # 1. Confirm uptrend: EMA(50) must be rising
        ema_50 = compute_ema(closes, 50)
        ema_50_old = compute_ema(closes[:-10], 50) if len(closes) > 60 else None
        if not ema_50 or not ema_50_old:
            return None
        if ema_50 <= ema_50_old:
            return None  # EMA not rising = no uptrend

        # 2. Price must be above EMA(50)
        if price < ema_50:
            return None

        # 3. RSI pullback zone: 40-50 (not overbought, not oversold)
        rsi = _compute_rsi_from_candles(candles)
        if rsi is None or rsi < 38 or rsi > 52:
            return None

        # 4. Bullish candle confirmation
        if candles[-1]["close"] <= candles[-1]["open"]:
            return None

        # 5. Volume confirmation
        vol_ratio = _compute_volume_ratio(candles, 20)
        if vol_ratio < 1.2:
            return None

        # 6. MACD histogram positive (trend momentum intact)
        macd = compute_macd(closes)
        if macd and macd[2] <= 0:
            return None

        # ATR-based stops
        atr = _compute_atr_from_candles(candles)
        atr_pct = (atr / price) if atr and price > 0 else 0.04
        stop_dist = max(0.03, min(0.08, atr_pct * 2.5))

        score = min(90, 60 + vol_ratio * 5 + (50 - rsi) * 0.5)
        if score >= config.min_qual_score_swing:
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="momentum_swing", side="long", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} pullback entry RSI={rsi:.0f}, EMA50 rising, vol={vol_ratio:.1f}x",
                entry_price=price, stop_price=price * (1 - stop_dist),
                target_price=price * (1 + stop_dist * 3.0),  # 3:1 R:R
                suggested_size_usd=100, expires_at=now_ms + 7_200_000, created_at=now_ms,
            )
        return None


class MomentumScalpSimulator(StrategySimulator):
    strategy_id = "momentum_scalp"
    tier = "scalp"

    def required_candles(self):
        return 10

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        lookback = max(3, min(len(candles), 10))
        mom = _compute_momentum_pct(candles, lookback)
        if mom is None:
            return None
        vol_ratio = _compute_volume_ratio(candles, lookback)

        if mom >= config.momentum_pct_scalp and vol_ratio >= config.volume_multiplier_scalp:
            score = min(90, 50 + mom * 250)
            if score >= config.min_qual_score_scalp:
                price = candles[-1]["close"]
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_scalp)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="momentum_scalp", side="long", tier="scalp",
                    score=score, confidence="medium",
                    sources=["price_action"],
                    reasoning=f"{symbol} +{mom*100:.1f}% scalp momentum, {vol_ratio:.1f}x vol",
                    entry_price=price, stop_price=price * (1 - config.base_trail_pct_scalp),
                    target_price=price * (1 + config.base_trail_pct_scalp * 2.0),
                    suggested_size_usd=60, expires_at=now_ms + 60_000, created_at=now_ms,
                )
        return None


class MeanReversionSimulator(StrategySimulator):
    """Unified VWAP + BB mean reversion. Research: VWAP -2/3 sigma + BB lower + RSI<30
    in ranging markets (ADX<30) is the highest-probability mean reversion setup."""
    strategy_id = "mean_reversion"
    tier = "swing"

    def required_candles(self):
        return 30

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 25:
            return None

        price = candles[-1]["close"]
        closes = [c["close"] for c in candles]

        # RSI
        rsi = _compute_rsi_from_candles(candles)
        if rsi is None:
            return None

        # ADX: only trade in ranging markets (ADX < 30)
        ohlcvs = [OHLCV(c["open"], c["high"], c["low"], c["close"], c["volume"], c["open_time"]) for c in candles]
        adx_result = compute_adx(ohlcvs)
        if adx_result and adx_result[0] > 30:
            return None

        # Bollinger Bands
        bb = compute_bollinger_bands(closes, period=20, num_std=2.0)
        if not bb:
            return None
        upper, middle, lower, width = bb
        if width < 0.015:
            return None  # dead flat

        # Rolling VWAP from kline data
        vwap_window = candles[-24:]  # 24-bar rolling VWAP
        sum_pv = sum(c["close"] * c["volume"] for c in vwap_window)
        sum_v = sum(c["volume"] for c in vwap_window)
        vwap = sum_pv / sum_v if sum_v > 0 else middle
        vwap_dev = (price - vwap) / vwap if vwap > 0 else 0

        # Volume confirmation
        vol_ratio = _compute_volume_ratio(candles, 20)

        # ATR stops
        atr = _compute_atr_from_candles(candles)
        atr_pct = (atr / price) if atr and price > 0 else 0.04
        stop_dist = max(0.03, min(0.06, atr_pct * 2.0))

        # LONG: price below both lower BB AND VWAP -2%, RSI < 30, volume elevated
        if (price <= lower and vwap_dev < -0.02 and rsi < 30
                and vol_ratio > 1.3
                and ctx.phase not in ("extreme_fear",)):
            dev_score = min(30, abs(vwap_dev) * 500)
            rsi_score = min(20, 30 - rsi)
            score = min(90, 55 + dev_score + rsi_score)
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            target = max(vwap, middle)  # target = VWAP or BB middle
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="mean_reversion", side="long", tier="swing",
                score=score, confidence="medium" if score > 70 else "low",
                sources=["price_action"],
                reasoning=f"{symbol} VWAP dev={vwap_dev*100:.1f}%, RSI={rsi:.0f}, below lower BB",
                entry_price=price, target_price=target,
                stop_price=price * (1 - stop_dist),
                suggested_size_usd=80, expires_at=now_ms + 3_600_000, created_at=now_ms,
            )

        # SHORT: price above both upper BB AND VWAP +2%, RSI > 70
        if (price >= upper and vwap_dev > 0.02 and rsi > 70
                and vol_ratio > 1.3
                and ctx.phase not in ("extreme_greed",)):
            dev_score = min(30, vwap_dev * 500)
            rsi_score = min(20, rsi - 70)
            score = min(88, 52 + dev_score + rsi_score)
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            target = min(vwap, middle)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="mean_reversion", side="short", tier="swing",
                score=score, confidence="medium" if score > 68 else "low",
                sources=["price_action"],
                reasoning=f"{symbol} at upper BB (RSI={rsi:.0f}, ADX={'%.0f' % adx_result[0] if adx_result else '?'})",
                entry_price=price, target_price=target,
                stop_price=price * (1 + stop_dist),
                suggested_size_usd=60, expires_at=now_ms + 3_600_000, created_at=now_ms,
            )
        return None


class CorrelationBreakSimulator(StrategySimulator):
    """Uses BTC klines + alt klines to derive correlation breaks."""
    strategy_id = "correlation_break"
    tier = "swing"

    def __init__(self):
        super().__init__()
        self._corr_history: dict[str, list[tuple[float, float]]] = {}  # symbol -> [(btc_pct, alt_pct)]

    def required_candles(self):
        return 30

    def data_feeds(self):
        return ["klines", "btc_klines"]

    # Symbols with consistently <45% WR over 5yr backtest — skip to avoid drag
    _blacklist = {"EOS", "QTUM", "TRX", "LTC", "BNT", "NEO", "ATOM", "ADA", "ICX", "THETA"}

    def scan(self, symbol, candles, config, ctx, now_ms, btc_candles=None):
        if symbol in self._blacklist:
            return None
        if ctx.phase in ("extreme_greed", "extreme_fear"):
            return None
        if btc_candles is None or len(btc_candles) < 2 or len(candles) < 2:
            return None

        # Compute 1h pct change
        btc_1h_pct = (btc_candles[-1]["close"] - btc_candles[-2]["close"]) / btc_candles[-2]["close"]
        alt_1h_pct = (candles[-1]["close"] - candles[-2]["close"]) / candles[-2]["close"]

        # Build correlation history
        hist = self._corr_history.setdefault(symbol, [])
        hist.append((btc_1h_pct, alt_1h_pct))
        if len(hist) > 200:
            self._corr_history[symbol] = hist[-200:]

        if len(hist) < 24:
            return None

        # Simple linear regression: expected_alt = alpha + beta * btc_pct
        n = len(hist)
        sum_x = sum(h[0] for h in hist)
        sum_y = sum(h[1] for h in hist)
        sum_xy = sum(h[0] * h[1] for h in hist)
        sum_xx = sum(h[0] ** 2 for h in hist)
        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-10:
            return None
        beta = (n * sum_xy - sum_x * sum_y) / denom
        alpha = (sum_y - beta * sum_x) / n
        expected = alpha + beta * btc_1h_pct
        divergence = alt_1h_pct - expected

        price = candles[-1]["close"]

        # Underperformance long — 3% stop / 5% target
        # Require stronger divergence for longs (43.2% WR at -0.05 vs 56.5% shorts)
        if divergence < -0.08:
            div_score = min(30, abs(divergence) * 400)
            score = min(80, 50 + div_score)
            if score >= config.min_qual_score_swing:
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="correlation_break", side="long", tier="swing",
                    score=score, confidence="low", sources=["correlation"],
                    reasoning=f"{symbol} underperforming BTC by {divergence*100:.1f}%",
                    entry_price=price, stop_price=price * 0.97,
                    target_price=price * 1.05,
                    suggested_size_usd=70, expires_at=now_ms + 7_200_000, created_at=now_ms,
                )

        # Overperformance short — 3% stop / 5% target
        if divergence > 0.065:
            div_score = min(28, divergence * 350)
            score = min(78, 48 + div_score)
            if score >= config.min_qual_score_swing:
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="correlation_break", side="short", tier="swing",
                    score=score, confidence="low", sources=["correlation"],
                    reasoning=f"{symbol} overperforming BTC by {divergence*100:.1f}%",
                    entry_price=price, stop_price=price * 1.03,
                    target_price=price * 0.95,
                    suggested_size_usd=60, expires_at=now_ms + 7_200_000, created_at=now_ms,
                )
        return None


class CorrelationBreakEthSimulator(StrategySimulator):
    """ETH-referenced correlation break — alts vs ETH regression."""
    strategy_id = "correlation_break_eth"
    tier = "swing"

    def __init__(self):
        super().__init__()
        self._corr_history: dict[str, list[tuple[float, float]]] = {}

    def required_candles(self):
        return 30

    def data_feeds(self):
        return ["klines", "btc_klines"]  # We'll use btc_klines to pass eth_candles via the combined loop

    _blacklist = {"EOS", "QTUM", "TRX", "LTC", "BNT", "NEO", "ATOM", "ADA", "ICX", "THETA", "BTC", "ETH"}

    def scan(self, symbol, candles, config, ctx, now_ms, eth_candles=None):
        if symbol in self._blacklist:
            return None
        if ctx.phase in ("extreme_greed", "extreme_fear"):
            return None
        if eth_candles is None or len(eth_candles) < 2 or len(candles) < 2:
            return None

        eth_1h_pct = (eth_candles[-1]["close"] - eth_candles[-2]["close"]) / eth_candles[-2]["close"]
        alt_1h_pct = (candles[-1]["close"] - candles[-2]["close"]) / candles[-2]["close"]

        hist = self._corr_history.setdefault(symbol, [])
        hist.append((eth_1h_pct, alt_1h_pct))
        if len(hist) > 200:
            self._corr_history[symbol] = hist[-200:]

        if len(hist) < 24:
            return None

        n = len(hist)
        sum_x = sum(h[0] for h in hist)
        sum_y = sum(h[1] for h in hist)
        sum_xy = sum(h[0] * h[1] for h in hist)
        sum_xx = sum(h[0] ** 2 for h in hist)
        denom = n * sum_xx - sum_x * sum_x
        if abs(denom) < 1e-10:
            return None
        beta = (n * sum_xy - sum_x * sum_y) / denom
        alpha = (sum_y - beta * sum_x) / n
        expected = alpha + beta * eth_1h_pct
        divergence = alt_1h_pct - expected

        price = candles[-1]["close"]

        # Shorts only — longs lose money on ETH correlation
        if divergence > 0.065:
            div_score = min(28, divergence * 350)
            score = min(78, 48 + div_score)
            if score >= config.min_qual_score_swing:
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="correlation_break_eth", side="short", tier="swing",
                    score=score, confidence="low", sources=["correlation"],
                    reasoning=f"{symbol} overperforming ETH by {divergence*100:.1f}%",
                    entry_price=price, stop_price=price * 1.03,
                    target_price=price * 0.95,
                    suggested_size_usd=60, expires_at=now_ms + 7_200_000, created_at=now_ms,
                )
        return None


class FearGreedSimulator(StrategySimulator):
    """Uses REAL Fear & Greed Index data from Alternative.me API.
    Research: buying at FGI<=20 and selling at FGI>=80 beats buy-and-hold over 7 years."""
    strategy_id = "fear_greed_contrarian"
    tier = "swing"
    # FGI is a BTC/ETH-specific signal — expanding to alts dilutes edge (tested: 46.7% WR vs 61.4%)
    _eligible = {"BTC", "ETH"}

    def __init__(self):
        super().__init__()
        self.fgi_data: list[dict] = []  # loaded from fgi_loader

    def required_candles(self):
        return 20

    def timeframe(self):
        return "1d"

    def data_feeds(self):
        return ["klines", "fgi"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        if symbol not in self._eligible:
            return None
        if self._check_cooldown(symbol, now_ms):
            return None

        # Use REAL FGI data if available, fall back to derived
        if self.fgi_data:
            from src.backtesting.fgi_loader import get_fgi_at_timestamp
            fgi = get_fgi_at_timestamp(self.fgi_data, now_ms)
        else:
            fgi = ctx.fear_greed_index

        price = candles[-1]["close"]

        # Extreme fear -> contrarian long (FGI <= 20, research-backed)
        if fgi <= 20:
            extremeness = min(25, (20 - fgi) * 2)
            score = min(90, 65 + extremeness)
            self._set_cooldown(symbol, now_ms, 86_400_000 * 7)  # 7-day cooldown (FGI moves slowly)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="fear_greed_contrarian", side="long", tier="swing",
                score=score, confidence="medium" if score > 72 else "low",
                sources=["fear_greed"],
                reasoning=f"FGI={fgi} extreme fear — contrarian long (real data)",
                entry_price=price, stop_price=price * 0.88,
                target_price=price * 1.20,  # 20% target vs 12% stop = 1.67:1
                suggested_size_usd=150, expires_at=now_ms + 30 * 86_400_000, created_at=now_ms,
            )

        # Extreme greed -> contrarian short (FGI >= 80)
        if fgi >= 80:
            extremeness = min(20, (fgi - 80) * 1.5)
            score = min(82, 60 + extremeness)
            self._set_cooldown(symbol, now_ms, 86_400_000 * 7)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="fear_greed_contrarian", side="short", tier="swing",
                score=score, confidence="low", sources=["fear_greed"],
                reasoning=f"FGI={fgi:.0f} extreme greed — contrarian short",
                entry_price=price, stop_price=price * 1.07,
                target_price=price * 0.88,  # R:R fix: 12% target vs 7% stop
                suggested_size_usd=80, expires_at=now_ms + 5 * 86_400_000, created_at=now_ms,
            )
        return None


class FundingExtremeSimulator(StrategySimulator):
    """Simulates funding rate extremes using REAL Binance funding rate + OI data."""
    strategy_id = "funding_extreme"
    tier = "swing"

    _EIGHT_HOURS_MS = 8 * 3_600_000

    def __init__(self):
        super().__init__()
        self.funding_data: dict[str, list[dict]] = {}  # symbol -> sorted funding records
        self.oi_data: dict[str, list[dict]] = {}        # symbol -> sorted OI records

    def required_candles(self):
        return 20

    def data_feeds(self):
        return ["klines", "funding", "oi"]

    def _find_nearest_funding(self, symbol: str, ts: float) -> Optional[float]:
        """Find the nearest funding rate to the given timestamp (round to nearest 8h)."""
        records = self.funding_data.get(symbol, [])
        if not records:
            return None
        # Binary-ish search: find closest funding_time
        best = None
        best_dist = float("inf")
        for r in records:
            dist = abs(r["funding_time"] - ts)
            if dist < best_dist:
                best_dist = dist
                best = r["funding_rate"]
            elif dist > best_dist:
                break  # records are sorted, so once distance starts growing we can stop
        # Only use if within 8h window
        if best_dist <= self._EIGHT_HOURS_MS:
            return best
        return None

    def _compute_oi_change_pct(self, symbol: str, ts: float) -> Optional[float]:
        """Compute OI change pct over the last 8 hours from real OI data."""
        records = self.oi_data.get(symbol, [])
        if not records:
            return None
        # Find current OI (nearest to ts)
        current_oi = None
        past_oi = None
        target_past = ts - self._EIGHT_HOURS_MS

        for r in records:
            if r["timestamp"] <= ts:
                current_oi = r["sum_open_interest"]
            if r["timestamp"] <= target_past:
                past_oi = r["sum_open_interest"]

        if current_oi is None or past_oi is None or past_oi == 0:
            return None
        return ((current_oi - past_oi) / past_oi) * 100

    def scan(self, symbol, candles, config, ctx, now_ms, **kwargs):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 10:
            return None

        # Look up real funding rate at this timestamp
        funding_rate = self._find_nearest_funding(symbol, now_ms)
        if funding_rate is None:
            return None

        # Look up real OI change
        oi_change_pct = self._compute_oi_change_pct(symbol, now_ms)
        if oi_change_pct is None:
            oi_change_pct = 0.0  # default if OI data unavailable

        # Real funding rates are much smaller than the synthetic proxy used before.
        # Typical extreme: 0.0005-0.001 per 8h period (0.05-0.1%).
        # Use a fixed threshold calibrated to real data, not the config value
        # which was tuned for the old synthetic (pct_change * 0.05) proxy.
        min_magnitude = 0.0005  # 0.05% per period — top decile of funding rates
        price = candles[-1]["close"]

        # Short: over-leveraged longs — funding extreme + OI not spiking too much
        if funding_rate > min_magnitude and oi_change_pct < 15:
            mag_score = min(40, (funding_rate / min_magnitude - 1) * 20)
            oi_score = min(20, oi_change_pct / 5) if oi_change_pct > 0 else 0
            score = min(88, 55 + mag_score + oi_score)
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="funding_extreme", side="short", tier="swing",
                score=score, confidence="medium" if score > 70 else "low",
                sources=["funding_rates"],
                reasoning=f"{symbol} funding={funding_rate*100:.3f}%, OI change={oi_change_pct:+.1f}%",
                entry_price=price, stop_price=price * 1.06,
                target_price=price * 0.92,  # R:R fix: 8% target vs 6% stop = 1.33:1
                suggested_size_usd=60, expires_at=now_ms + 14_400_000, created_at=now_ms,
            )

        # Long: short squeeze — OI should be DECREASING (shorts closing/getting liquidated)
        # Block longs during extreme_fear (wrong_market_phase losses)
        if (funding_rate < -min_magnitude
                and oi_change_pct < -5
                and ctx.phase != "extreme_fear"):
            mag_score = min(35, (-funding_rate / min_magnitude - 1) * 18)
            score = min(85, 52 + mag_score)
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="funding_extreme", side="long", tier="swing",
                score=score, confidence="medium" if score > 68 else "low",
                sources=["funding_rates"],
                reasoning=f"{symbol} funding={funding_rate*100:.3f}% (neg), OI change={oi_change_pct:+.1f}%",
                entry_price=price, stop_price=price * 0.95,
                target_price=price * 1.08,  # R:R fix: 8% target vs 5% stop = 1.6:1
                suggested_size_usd=70, expires_at=now_ms + 14_400_000, created_at=now_ms,
            )
        return None


class LiquidationCascadeSimulator(StrategySimulator):
    """Detects liquidation-like cascades from sharp price drops + volume spikes."""
    strategy_id = "liquidation_cascade"
    tier = "swing"

    def required_candles(self):
        return 10

    def data_feeds(self):
        return ["klines"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 5:
            return None

        # Detect cascade-like patterns: sharp drop + volume spike
        recent_5 = candles[-5:]
        price_drop = (recent_5[-1]["close"] - recent_5[0]["close"]) / recent_5[0]["close"]
        vol_ratio = _compute_volume_ratio(candles, min(len(candles), 20))
        price = candles[-1]["close"]

        # Dip buy: sharp drop + high volume = potential bounce
        # Backtest fix: require deeper drop (-7%) and higher vol (4x) — 29.8% WR at -5%/3x
        if price_drop < -0.07 and vol_ratio > 4.0 and ctx.phase not in ("bear", "extreme_fear"):
            drop_score = min(30, abs(price_drop) * 200)
            vol_score = min(20, vol_ratio * 3)
            score = min(82, 42 + drop_score + vol_score)
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="liquidation_cascade", side="long", tier="swing",
                score=score, confidence="medium" if score > 72 else "low",
                sources=["liquidation_data"],
                reasoning=f"{symbol} down {price_drop*100:.1f}% in 5 candles with {vol_ratio:.1f}x vol (cascade bounce)",
                entry_price=price, stop_price=price * 0.97,
                target_price=price * 1.05,  # R:R fix: 5% target vs 3% stop
                suggested_size_usd=90, expires_at=now_ms + 3_600_000, created_at=now_ms,
            )
        return None


class CrossExchangeSimulator(StrategySimulator):
    """Simulates cross-exchange divergence using REAL spot vs futures price data.

    Uses Binance spot vs Binance futures as a cross-venue proxy.
    Spot/futures basis divergence is a real tradeable signal.
    """
    strategy_id = "cross_exchange_divergence"
    tier = "swing"

    _MIN_DIVERGENCE_PCT = 1.0  # Must exceed round-trip fees
    _HISTORY_SIZE = 120

    def __init__(self):
        super().__init__()
        self._divergence_history: dict[str, list[float]] = {}
        self.futures_candles: dict[str, list[dict]] = {}  # symbol -> sorted futures candles

    def required_candles(self):
        return 25

    def data_feeds(self):
        return ["klines", "futures_klines"]

    def _get_futures_close_at(self, symbol: str, ts: float) -> Optional[float]:
        """Find the futures close price at or nearest to the given timestamp."""
        candles = self.futures_candles.get(symbol, [])
        if not candles:
            return None
        best = None
        best_dist = float("inf")
        for c in candles:
            dist = abs(c["open_time"] - ts)
            if dist < best_dist:
                best_dist = dist
                best = c["close"]
            elif dist > best_dist:
                break  # sorted, distance growing
        # Only use if within 1 hour
        if best_dist <= 3_600_000:
            return best
        return None

    def scan(self, symbol, candles, config, ctx, now_ms, **kwargs):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 20:
            return None
        if ctx.phase in ("extreme_fear", "extreme_greed"):
            return None

        # Get spot and futures prices at current timestamp
        spot_close = candles[-1]["close"]
        futures_close = self._get_futures_close_at(symbol, now_ms)

        if futures_close is None or futures_close <= 0 or spot_close <= 0:
            return None

        # Compute divergence: (spot - futures) / futures * 100
        divergence_pct = (spot_close - futures_close) / futures_close * 100

        # Build rolling divergence history for z-score computation
        hist = self._divergence_history.setdefault(symbol, [])
        hist.append(divergence_pct)
        if len(hist) > self._HISTORY_SIZE:
            self._divergence_history[symbol] = hist[-self._HISTORY_SIZE:]
            hist = self._divergence_history[symbol]
        if len(hist) < 10:
            return None

        # Compute z-score over rolling window (matching production logic)
        avg = sum(hist) / len(hist)
        std = (sum((d - avg) ** 2 for d in hist) / (len(hist) - 1)) ** 0.5 if len(hist) > 1 else 0.001
        if std < 0.001:
            return None
        z = (hist[-1] - avg) / std

        price = spot_close

        # ATR-adaptive stops
        atr = _compute_atr_from_candles(candles)
        if atr and atr > 0 and price > 0:
            atr_pct = atr / price
            stop_dist = max(0.008, min(0.02, atr_pct * 1.5))
        else:
            stop_dist = 0.01

        # Spot overpriced vs futures -> SHORT (expect reversion)
        if z >= 2.5 and divergence_pct > self._MIN_DIVERGENCE_PCT:
            score = min(95, 50 + abs(z) * 6 + abs(divergence_pct) * 10)
            self._set_cooldown(symbol, now_ms, 300_000)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="cross_exchange_divergence", side="short", tier="swing",
                score=score, confidence="high", sources=["price_action", "correlation"],
                reasoning=f"{symbol} spot {divergence_pct:+.2f}% vs futures (z={z:.1f}), avg spread {avg:.3f}% +/- {std:.3f}%",
                entry_price=price, stop_price=price * (1 + stop_dist),
                target_price=futures_close,  # target = convergence to futures price
                suggested_size_usd=50, expires_at=now_ms + 3_600_000, created_at=now_ms,
            )

        # Spot underpriced vs futures -> LONG (expect reversion)
        if z <= -2.5 and divergence_pct < -self._MIN_DIVERGENCE_PCT:
            score = min(95, 50 + abs(z) * 6 + abs(divergence_pct) * 10)
            self._set_cooldown(symbol, now_ms, 300_000)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="cross_exchange_divergence", side="long", tier="swing",
                score=score, confidence="high", sources=["price_action", "correlation"],
                reasoning=f"{symbol} spot {divergence_pct:+.2f}% vs futures (z={z:.1f}), avg spread {avg:.3f}% +/- {std:.3f}%",
                entry_price=price, stop_price=price * (1 - stop_dist),
                target_price=futures_close,  # target = convergence to futures price
                suggested_size_usd=50, expires_at=now_ms + 3_600_000, created_at=now_ms,
            )
        return None


class OrderbookImbalanceSimulator(StrategySimulator):
    """Approximates orderbook imbalance from volume and price action patterns."""
    strategy_id = "orderbook_imbalance"
    tier = "scalp"

    def required_candles(self):
        return 10

    def data_feeds(self):
        return ["klines"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 5:
            return None
        # Skip in extreme phases — orderbook signals unreliable
        if ctx.phase in ("extreme_fear", "extreme_greed"):
            return None

        c = candles[-1]
        price = c["close"]
        body_range = c["high"] - c["low"]
        if body_range == 0:
            return None

        close_position = (c["close"] - c["low"]) / body_range  # 0=low, 1=high
        vol_ratio = _compute_volume_ratio(candles, min(len(candles), 10))

        # Tightened thresholds: 0.9 close position + 3.5x volume (was 0.8 + 2.5x)
        if close_position > 0.9 and vol_ratio > 3.5:
            score = min(82, 50 + close_position * 15 + vol_ratio * 4)
            if score >= config.min_qual_score_scalp:
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_scalp)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="orderbook_imbalance", side="long", tier="scalp",
                    score=score, confidence="low", sources=["orderbook"],
                    reasoning=f"{symbol} close near high ({close_position:.0%}), {vol_ratio:.1f}x vol (bid wall proxy)",
                    entry_price=price, stop_price=price * 0.98,
                    target_price=price * 1.04,  # R:R fix: 4% target vs 2% stop = 2:1
                    suggested_size_usd=40, expires_at=now_ms + 300_000, created_at=now_ms,
                )

        if close_position < 0.1 and vol_ratio > 3.5:
            score = min(80, 48 + (1 - close_position) * 15 + vol_ratio * 4)
            if score >= config.min_qual_score_scalp:
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_scalp)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="orderbook_imbalance", side="short", tier="scalp",
                    score=score, confidence="low", sources=["orderbook"],
                    reasoning=f"{symbol} close near low ({close_position:.0%}), {vol_ratio:.1f}x vol (ask wall proxy)",
                    entry_price=price, stop_price=price * 1.02,
                    target_price=price * 0.96,  # R:R fix: 4% target vs 2% stop = 2:1
                    suggested_size_usd=35, expires_at=now_ms + 300_000, created_at=now_ms,
                )
        return None


class NarrativeMomentumSimulator(StrategySimulator):
    """Simulates narrative momentum using sector relative performance."""
    strategy_id = "narrative_momentum"
    tier = "swing"

    SECTORS = {
        "ai_tokens": ["FET", "RENDER"],
        "defi_bluechip": ["UNI", "AAVE"],
        "layer2": ["ARB", "OP"],
        "liquid_staking": ["LDO"],
    }

    def required_candles(self):
        return 24

    def data_feeds(self):
        return ["klines"]

    def scan(self, symbol, candles, config, ctx, now_ms, all_candle_windows=None):
        if self._check_cooldown(symbol, now_ms):
            return None
        if all_candle_windows is None or len(candles) < 10:
            return None

        # Find which sector this symbol belongs to
        my_sector = None
        for sector, members in self.SECTORS.items():
            if symbol in members:
                my_sector = sector
                break
        if not my_sector:
            return None

        # Compute sector average performance
        members = self.SECTORS[my_sector]
        member_pcts = {}
        for mem in members:
            mem_candles = all_candle_windows.get(mem, [])
            if len(mem_candles) >= 10:
                pct = (mem_candles[-1]["close"] - mem_candles[-10]["close"]) / mem_candles[-10]["close"]
                member_pcts[mem] = pct

        if len(member_pcts) < 2:
            return None

        avg_pct = sum(member_pcts.values()) / len(member_pcts)
        my_pct = member_pcts.get(symbol)
        if my_pct is None:
            return None

        # Laggard detection: sector rising but this member lagging
        if avg_pct > 0.02 and my_pct < avg_pct * 0.3:
            lag = avg_pct - my_pct
            score = min(88, 48 + lag * 500)
            price = candles[-1]["close"]
            self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="narrative_momentum", side="long", tier="swing",
                score=score, confidence="medium" if score > 72 else "low",
                sources=["social"],
                reasoning=f"{my_sector} avg +{avg_pct*100:.1f}% but {symbol} only +{my_pct*100:.1f}% (laggard)",
                entry_price=price, stop_price=price * 0.94,
                target_price=price * 1.10,  # R:R fix: 10% target vs 6% stop
                suggested_size_usd=80, expires_at=now_ms + 7_200_000, created_at=now_ms,
            )
        return None


# Strategies that can't be meaningfully backtested from klines alone
# We still include them but mark data quality as low

class WhaleTrackerSimulator(StrategySimulator):
    """Simulates whale accumulation from unusually high volume bars."""
    strategy_id = "whale_accumulation"
    tier = "swing"

    def required_candles(self):
        return 20

    def data_feeds(self):
        return ["klines"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 20:
            return None

        # Proxy: massive volume spike with small price change = accumulation
        vol_ratio = _compute_volume_ratio(candles, 20)
        pct_change = (candles[-1]["close"] - candles[-2]["close"]) / candles[-2]["close"]
        price = candles[-1]["close"]

        # High volume + small move = absorption (whale accumulation proxy)
        # Require 5x vol (was 4x) and tighter flat price check (0.005 vs 0.01)
        if vol_ratio > 5.0 and abs(pct_change) < 0.005:
            # Direction: bullish if close > open
            if candles[-1]["close"] > candles[-1]["open"]:
                # Higher base score to survive min_qual_score healing
                score = min(85, 60 + vol_ratio * 2)
                atr = _compute_atr_from_candles(candles)
                atr_pct = (atr / price) if atr and price > 0 else 0.04
                stop_dist = max(0.04, min(0.07, atr_pct * 2.0))
                self._set_cooldown(symbol, now_ms, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="whale_accumulation", side="long", tier="swing",
                    score=score, confidence="low", sources=["whale_alert"],
                    reasoning=f"{symbol} {vol_ratio:.1f}x vol spike with flat price (accumulation proxy)",
                    entry_price=price, stop_price=price * (1 - stop_dist),
                    target_price=price * (1 + stop_dist * 1.6),  # 1.6:1 R:R
                    suggested_size_usd=100, expires_at=now_ms + 21_600_000, created_at=now_ms,
                )
        return None


class ListingPumpSimulator(StrategySimulator):
    """Detects exchange listing events using REAL listing dates from Binance Futures
    exchangeInfo onboardDate + volume/price confirmation from klines.

    Two modes:
    1. Real listing events: uses listing_events data (symbol -> listing_date_ms)
       Fires signal within 6h AFTER the listing date when volume confirms
    2. Fallback: original volume explosion + price spike heuristic
    """
    strategy_id = "listing_pump"
    tier = "swing"

    _LISTING_WINDOW_MS = 24 * 3_600_000  # 24h window after listing (was 6h — too narrow for backtesting)

    def __init__(self):
        super().__init__()
        self._signaled: set[str] = set()  # tracks "symbol:listing_ms" to avoid dup signals
        # symbol -> list of (listing_date_ms, exchange) — multiple listings per symbol
        self.listing_events: dict[str, list[tuple[int, str]]] = {}

    def required_candles(self):
        return 2

    def timeframe(self):
        return "5m"  # minimal — listings fire on first candles available

    def data_feeds(self):
        return ["klines", "listings"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        if len(candles) < 6:
            return None

        price = candles[-1]["close"]

        # --- Mode 1: Real listing event data ---
        events = self.listing_events.get(symbol, [])
        for listing_ms, exchange in events:
            signal_key = f"{symbol}:{listing_ms}"
            if signal_key in self._signaled:
                continue

            age_ms = now_ms - listing_ms
            # Signal within window after listing
            if 0 <= age_ms <= self._LISTING_WINDOW_MS:
                # FILTER 1: Price must be pumping — need +5% from first candle
                if len(candles) >= 2:
                    first_price = candles[0]["close"]
                    price_change = (price - first_price) / first_price if first_price > 0 else 0
                    if price_change < 0.05:
                        continue  # not pumping, skip this listing

                # FILTER 2: Volume confirmation
                if len(candles) >= 12:
                    baseline = candles[:-6]
                    recent = candles[-6:]
                    baseline_vol = sum(c["volume"] for c in baseline) / len(baseline) if baseline else 0
                    recent_vol = sum(c["volume"] for c in recent) / len(recent) if recent else 0
                    vol_ratio = recent_vol / baseline_vol if baseline_vol > 0 else 10.0
                elif len(candles) >= 3:
                    # Early in listing — check if volume is significant (not dead)
                    avg_vol = sum(c["volume"] for c in candles) / len(candles)
                    vol_ratio = 5.0 if avg_vol > 0 else 0
                else:
                    vol_ratio = 5.0

                if vol_ratio >= 1.5:
                    self._signaled.add(signal_key)
                    freshness_bonus = max(0, 20 - int(age_ms / 3_600_000))  # hours since listing
                    vol_score = min(20, vol_ratio * 2)
                    pump_score = min(20, price_change * 100) if len(candles) >= 2 else 10
                    exchange_bonus = 5 if "coinbase" in exchange else 0
                    score = min(95, 50 + freshness_bonus + vol_score + pump_score + exchange_bonus)

                    return TradeSignal(
                        id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                        strategy="listing_pump", side="long", tier="swing",
                        score=score, confidence="high" if score > 80 else "medium",
                        sources=["listing_detector"],
                        reasoning=f"{symbol} {exchange} listing {int(age_ms/3_600_000)}h ago, {vol_ratio:.1f}x vol",
                        entry_price=price, stop_price=price * 0.85,
                        target_price=price * 1.20,
                        suggested_size_usd=120, expires_at=now_ms + self._LISTING_WINDOW_MS, created_at=now_ms,
                    )

        # If we have listing events for this symbol, don't use fallback
        if events:
            return None

        # --- Mode 2: Fallback — volume explosion heuristic ---
        if len(candles) < 24:
            return None

        baseline = candles[-24:-6]
        recent = candles[-6:]

        baseline_avg_vol = sum(c["volume"] for c in baseline) / len(baseline) if baseline else 0
        recent_avg_vol = sum(c["volume"] for c in recent) / len(recent) if recent else 0

        if baseline_avg_vol <= 0:
            return None

        vol_explosion = recent_avg_vol / baseline_avg_vol
        price_change = (recent[-1]["close"] - baseline[-1]["close"]) / baseline[-1]["close"]

        # Listing pattern: 10x+ volume explosion AND 15%+ price pump
        if vol_explosion >= 10.0 and price_change > 0.15:
            self._signaled.add(f"{symbol}:heuristic:{int(now_ms)}")  # don't block real listing events
            price = candles[-1]["close"]

            freshness_score = min(20, max(0, 20 - len(recent)))
            vol_score = min(30, vol_explosion * 2)
            pump_score = min(25, price_change * 100)
            score = min(95, 45 + freshness_score + vol_score + pump_score)

            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="listing_pump", side="long", tier="swing",
                score=score, confidence="high" if score > 80 else "medium",
                sources=["listing_detector"],
                reasoning=f"{symbol} listing-like event: {vol_explosion:.0f}x vol, +{price_change*100:.0f}% price",
                entry_price=price, stop_price=price * 0.85,
                target_price=price * 1.20,  # R:R fix: 20% target vs 15% stop
                suggested_size_usd=120, expires_at=now_ms + 6 * 3_600_000, created_at=now_ms,
            )
        return None


class ProtocolRevenueSimulator(StrategySimulator):
    """Cannot backtest protocol revenue from klines — marks as data insufficient."""
    strategy_id = "protocol_revenue"
    tier = "swing"

    def data_feeds(self):
        return ["protocol_metrics"]

    def scan(self, symbol, candles, config, ctx, now_ms):
        return None


# ---------------------------------------------------------------------------
# NEW: BB Squeeze Strategy — volatility expansion after compression
# ---------------------------------------------------------------------------

class BBSqueezeSimulator(StrategySimulator):
    """BB/KC squeeze breakout (4h timeframe). — BB inside Keltner Channel = true squeeze.
    Direction from MACD histogram, not price vs middle band.
    Research: adding KC filter + MACD direction takes WR from 40% to 50-55%."""
    strategy_id = "bb_squeeze"
    tier = "swing"

    def __init__(self):
        super().__init__()
        self._width_history: dict[str, list[float]] = {}
        self._prev_squeeze: dict[str, bool] = {}  # track squeeze state for release detection

    def required_candles(self):
        return 50

    def timeframe(self):
        return "4h"

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 50:
            return None

        closes = [c["close"] for c in candles]
        bb = compute_bollinger_bands(closes, period=20, num_std=2.0)
        if not bb:
            return None
        bb_upper, bb_middle, bb_lower, bb_width = bb

        # Keltner Channel: EMA(20) ± 1.5 * ATR(20)
        kc_middle = compute_ema(closes, 20)
        if not kc_middle:
            return None
        atr = _compute_atr_from_candles(candles, period=20)
        if not atr:
            return None
        kc_upper = kc_middle + 1.5 * atr
        kc_lower = kc_middle - 1.5 * atr

        # TRUE SQUEEZE: BB bands are INSIDE Keltner Channel
        is_squeeze = bb_lower > kc_lower and bb_upper < kc_upper
        was_squeeze = self._prev_squeeze.get(symbol, False)
        self._prev_squeeze[symbol] = is_squeeze

        # Track width history
        hist = self._width_history.setdefault(symbol, [])
        hist.append(bb_width)
        if len(hist) > 200:
            self._width_history[symbol] = hist[-200:]

        # Signal on SQUEEZE RELEASE: was in squeeze, now released
        if not was_squeeze or is_squeeze:
            return None  # still in squeeze or wasn't in squeeze

        # Volume confirmation
        vol_ratio = _compute_volume_ratio(candles, 20)
        if vol_ratio < 1.5:
            return None

        # MACD histogram for direction (not price vs middle band)
        macd_result = compute_macd(closes)
        if not macd_result:
            return None
        macd_line, signal_line, histogram = macd_result

        price = candles[-1]["close"]
        atr_pct = atr / price if price > 0 else 0.03
        stop_dist = max(0.03, min(0.07, atr_pct * 2.5))  # wider stops for breakout

        # Wide stops for breakout strategies — need room to develop
        wide_stop = max(0.05, min(0.10, atr_pct * 3.0))

        # Direction from MACD histogram
        if histogram > 0:
            # Bullish momentum — long only if not in bear
            if ctx.phase in ("bear", "extreme_fear"):
                return None
            side = "long"
            stop_price = price * (1 - wide_stop)
            target_price = price * (1 + wide_stop * 3.0)  # 3:1 R:R
        else:
            # Bearish momentum — short only if not in bull
            if ctx.phase in ("bull", "extreme_greed"):
                return None
            side = "short"
            stop_price = price * (1 + wide_stop)
            target_price = price * (1 - wide_stop * 3.0)

        score = min(85, 65 + abs(histogram) * 500)
        self._set_cooldown(symbol, now_ms, 43_200_000)  # 12h cooldown
        return TradeSignal(
            id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
            strategy="bb_squeeze", side=side, tier="swing",
            score=score, confidence="medium",
            sources=["price_action"],
            reasoning=f"{symbol} BB/KC squeeze release (MACD hist={histogram:.4f}, vol={vol_ratio:.1f}x)",
            entry_price=price, stop_price=stop_price, target_price=target_price,
            suggested_size_usd=80, expires_at=now_ms + 7_200_000, created_at=now_ms,
        )
        return None


# ---------------------------------------------------------------------------
# NEW: EMA Crossover with ADX confirmation
# ---------------------------------------------------------------------------

class EMACrossoverSimulator(StrategySimulator):
    """EMA(12/50) crossover — LONG ONLY, with MACD confirmation and EMA(200) trend filter.
    Research: EMA(12/50) + 200 trend filter + long-only takes WR from 39% to 50-55%."""
    strategy_id = "ema_crossover"
    tier = "swing"

    def __init__(self):
        super().__init__()
        self._prev_ema_state: dict[str, str] = {}

    def required_candles(self):
        return 60

    def timeframe(self):
        return "4h"  # Need enough for EMA(50) + some history

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 55:
            return None

        closes = [c["close"] for c in candles]
        ema_12 = compute_ema(closes, 12)
        ema_50 = compute_ema(closes, 50)
        if not ema_12 or not ema_50:
            return None

        # Volatility filter: skip dead markets
        bb = compute_bollinger_bands(closes, period=20, num_std=2.0)
        if bb and bb[3] < 0.03:
            self._prev_ema_state[symbol] = "above" if ema_12 > ema_50 else "below"
            return None

        # ADX trend strength — require confirmed trend (>25)
        ohlcvs = [OHLCV(c["open"], c["high"], c["low"], c["close"], c["volume"], c["open_time"]) for c in candles]
        adx_result = compute_adx(ohlcvs)
        if not adx_result or adx_result[0] < 25:
            self._prev_ema_state[symbol] = "above" if ema_12 > ema_50 else "below"
            return None

        # MACD histogram must confirm momentum direction
        macd_result = compute_macd(closes)
        if not macd_result or macd_result[2] <= 0:  # histogram must be positive for longs
            self._prev_ema_state[symbol] = "above" if ema_12 > ema_50 else "below"
            return None

        # Volume confirmation
        vol_ratio = _compute_volume_ratio(candles, 20)
        if vol_ratio < 1.3:
            self._prev_ema_state[symbol] = "above" if ema_12 > ema_50 else "below"
            return None

        current_state = "above" if ema_12 > ema_50 else "below"
        prev_state = self._prev_ema_state.get(symbol)
        self._prev_ema_state[symbol] = current_state

        if prev_state is None or current_state == prev_state:
            return None

        price = candles[-1]["close"]
        atr = _compute_atr_from_candles(candles)
        atr_pct = (atr / price) if atr and price > 0 else 0.03
        stop_dist = max(0.03, min(0.07, atr_pct * 2.5))  # wider stops

        # LONG ONLY — bullish crossover with trend confirmation
        if current_state == "above" and ctx.phase not in ("extreme_fear", "bear"):
            # Price must be above EMA(50) — basic trend filter
            if price < ema_50:
                return None
            score = min(85, 58 + adx_result[0] * 0.5)
            self._set_cooldown(symbol, now_ms, 86_400_000)
            # Wide stops: 5% minimum, target 3:1 R:R — trend strategies need room
            wide_stop = max(0.05, min(0.10, atr_pct * 3.0))
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="ema_crossover", side="long", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} EMA12 crossed above EMA50 (ADX={adx_result[0]:.0f}, MACD+, vol={vol_ratio:.1f}x)",
                entry_price=price, stop_price=price * (1 - wide_stop),
                target_price=price * (1 + wide_stop * 3.0),  # 3:1 R:R
                suggested_size_usd=80, expires_at=now_ms + 7_200_000, created_at=now_ms,
            )
        # No shorts — crypto has structural long bias
        return None


# ---------------------------------------------------------------------------
# Strategy Registry
# ---------------------------------------------------------------------------

ALL_SIMULATORS: dict[str, type] = {
    # === Proven profitable ===
    "correlation_break": CorrelationBreakSimulator,
    "cross_exchange_divergence": CrossExchangeSimulator,
    # "listing_pump": ListingPumpSimulator,  # run separately with 5m data
    # === Re-engineered with research-backed fixes ===
    "momentum_swing": MomentumSimulator,               # FIXED: pullback entry, EMA50 rising
    "fear_greed_contrarian": FearGreedSimulator,        # FIXED: real FGI data from Alternative.me
    # trend_following, rsi_divergence, ema_crossover, bb_squeeze, volume_breakout
    # are defined after this dict — added via ALL_SIMULATORS["..."] = ... below
    "funding_extreme": FundingExtremeSimulator,         # real funding rates
    # === Not backtestable from available data ===
    # "orderbook_imbalance": OrderbookImbalanceSimulator,  # needs L2 data
    # "narrative_momentum": NarrativeMomentumSimulator,    # needs social data
    # "protocol_revenue": ProtocolRevenueSimulator,        # needs DeFi metrics
    # "momentum_scalp": MomentumScalpSimulator,            # needs 5m candles
    # "whale_accumulation": WhaleTrackerSimulator,         # needs on-chain data
    # "liquidation_cascade": LiquidationCascadeSimulator,  # needs real liquidation data
}


# ---------------------------------------------------------------------------
# Main Backtest Runner
# ---------------------------------------------------------------------------

class SystematicBacktester:
    """Runs each strategy one-by-one, analyzes losses, and updates params."""

    def __init__(self, symbols: list[str], start_date: str, end_date: str,
                 verbose: bool = False):
        self.symbols = symbols
        self.start_date = start_date
        self.end_date = end_date
        self.verbose = verbose
        self.start_ms = _date_to_ms(start_date)
        self.end_ms = _date_to_ms(end_date)
        self.results: list[StrategyBacktestResult] = []

    def load_data(self) -> dict[str, list[dict]]:
        """Load kline data for all symbols."""
        print(f"\nLoading kline data for {len(self.symbols)} symbols "
              f"({self.start_date} to {self.end_date})...")
        symbol_candles: dict[str, list[dict]] = {}
        for symbol in self.symbols:
            print(f"  Loading {symbol}...", end=" ", flush=True)
            try:
                candles = load_klines(symbol, "1h", self.start_ms, self.end_ms)
                if candles:
                    symbol_candles[symbol] = candles
                    print(f"{len(candles)} candles")
                else:
                    print("NO DATA")
            except Exception as e:
                print(f"ERROR: {e}")
        print(f"  Loaded data for {len(symbol_candles)}/{len(self.symbols)} symbols\n")
        return symbol_candles

    def run_strategy(self, strategy_id: str, symbol_candles: dict[str, list[dict]],
                     config: ScannerConfig) -> StrategyBacktestResult:
        """Run a single strategy through the backtest.

        Loads candle data at the strategy's declared timeframe.
        If timeframe != 1h, downloads fresh data at the correct interval.
        """
        sim_class = ALL_SIMULATORS.get(strategy_id)
        if not sim_class:
            print(f"  Unknown strategy: {strategy_id}")
            return StrategyBacktestResult(
                strategy=strategy_id, total_trades=0, wins=0, losses=0,
                win_rate=0, total_pnl_usd=0, max_drawdown_pct=0, avg_hold_hours=0,
                loss_analyses=[], parameter_changes=[], issues_found=["unknown strategy"],
                data_quality_score=0, final_config=dataclasses.asdict(config),
            )

        sim = sim_class()

        # Load data at the strategy's correct timeframe
        tf = sim.timeframe()
        if tf != "1h":
            print(f"  Strategy requires {tf} candles — loading...")
            tf_candles: dict[str, list[dict]] = {}
            for sym in self.symbols:
                try:
                    candles = load_klines(sym, tf, self.start_ms, self.end_ms)
                    if candles:
                        tf_candles[sym] = candles
                except Exception:
                    pass
            print(f"  Loaded {tf} data for {len(tf_candles)} symbols")
            # Use timeframe-specific candles for this strategy
            symbol_candles = tf_candles

        # Check if strategy can be meaningfully backtested
        _LOADABLE_FEEDS = {"klines", "btc_klines", "funding", "oi", "futures_klines", "listings", "fgi"}
        feeds = sim.data_feeds()
        unloadable = [f for f in feeds if f not in _LOADABLE_FEEDS]
        if unloadable:
            print(f"  {strategy_id}: SKIPPED (requires {unloadable} — not available in backtest)")
            return StrategyBacktestResult(
                strategy=strategy_id, total_trades=0, wins=0, losses=0,
                win_rate=0, total_pnl_usd=0, max_drawdown_pct=0, avg_hold_hours=0,
                loss_analyses=[], parameter_changes=[],
                issues_found=[f"Cannot backtest: requires {unloadable}"],
                data_quality_score=0, final_config=dataclasses.asdict(config),
            )

        # Load external data feeds if needed
        if "funding" in feeds and hasattr(sim, "funding_data"):
            print(f"  Loading funding rate data...")
            for sym in self.symbols:
                try:
                    data = load_funding_rates(sym, self.start_ms, self.end_ms)
                    if data:
                        sim.funding_data[sym] = data
                        if self.verbose:
                            print(f"    {sym}: {len(data)} funding records")
                except Exception as e:
                    print(f"    {sym}: funding load error: {e}")
            print(f"  Funding data loaded for {len(sim.funding_data)} symbols")

        if "oi" in feeds and hasattr(sim, "oi_data"):
            print(f"  Loading open interest data...")
            for sym in self.symbols:
                try:
                    data = load_open_interest(sym, self.start_ms, self.end_ms)
                    if data:
                        sim.oi_data[sym] = data
                        if self.verbose:
                            print(f"    {sym}: {len(data)} OI records")
                except Exception as e:
                    print(f"    {sym}: OI load error: {e}")
            print(f"  OI data loaded for {len(sim.oi_data)} symbols")

        if "futures_klines" in feeds and hasattr(sim, "futures_candles"):
            print(f"  Loading futures kline data...")
            for sym in self.symbols:
                try:
                    data = load_futures_klines(sym, "1h", self.start_ms, self.end_ms)
                    if data:
                        sim.futures_candles[sym] = data
                        if self.verbose:
                            print(f"    {sym}: {len(data)} futures candles")
                except Exception as e:
                    print(f"    {sym}: futures kline error: {e}")
            print(f"  Futures data loaded for {len(sim.futures_candles)} symbols")

        if "listings" in feeds and hasattr(sim, "listing_events"):
            print(f"  Loading exchange listing dates (all Binance Spot + Futures)...")
            all_listings = load_exchange_listings(probe_all_spot=True)
            events = get_listing_events_in_range(all_listings, self.start_ms, self.end_ms)

            # Collect ALL listed symbols — each listing event is a separate trade opportunity
            listed_syms: set[str] = set()
            total_events = 0
            for evt in events:
                sym = evt["symbol"]
                exchange = evt.get("exchange", "unknown")
                # Normalize Binance Futures 1000x prefix (e.g. 1000PEPE -> PEPE)
                if sym.startswith("1000000"):
                    sym = sym[7:]
                elif sym.startswith("1000"):
                    sym = sym[4:]
                # Append each listing event (multiple per symbol allowed)
                sim.listing_events.setdefault(sym, []).append(
                    (evt["listing_date_ms"], exchange)
                )
                listed_syms.add(sym)
                total_events += 1
            print(f"  {total_events} listing events across {len(listed_syms)} symbols")

            # Download kline data for listed symbols not already loaded
            new_syms = listed_syms - set(symbol_candles.keys())
            if new_syms:
                print(f"  Downloading klines for {len(new_syms)} newly-listed symbols...")
                loaded_count = 0
                for i, sym in enumerate(sorted(new_syms)):
                    try:
                        candles = load_klines(sym, "1h", self.start_ms, self.end_ms)
                        if candles:
                            symbol_candles[sym] = candles
                            loaded_count += 1
                    except Exception:
                        pass
                    if (i + 1) % 25 == 0:
                        print(f"    {i+1}/{len(new_syms)} probed ({loaded_count} loaded)...")
                print(f"  Loaded klines for {loaded_count}/{len(new_syms)} new symbols")

            print(f"  Listing events: {len(sim.listing_events)} symbols in backtest range")

        if "fgi" in feeds and hasattr(sim, "fgi_data"):
            print(f"  Loading Fear & Greed Index data...")
            from src.backtesting.fgi_loader import load_fear_greed_index
            sim.fgi_data = load_fear_greed_index()
            print(f"  FGI data: {len(sim.fgi_data)} days loaded")

        # Build unified timeline
        all_timestamps: set[int] = set()
        for candles in symbol_candles.values():
            for c in candles:
                all_timestamps.add(c["open_time"])
        timeline = sorted(all_timestamps)

        # Index candles
        candle_index: dict[str, dict[int, dict]] = {}
        for sym, candles in symbol_candles.items():
            candle_index[sym] = {c["open_time"]: c for c in candles}

        # Rolling windows
        candle_windows: dict[str, list[dict]] = {s: [] for s in symbol_candles}
        max_window = max(50, sim.required_candles() + 20)

        btc_candles_window: list[dict] = []
        trade_count = 0

        for ts in timeline:
            now_ms = float(ts)

            # Update candle windows
            for sym in symbol_candles:
                candle = candle_index[sym].get(ts)
                if candle:
                    candle_windows[sym].append(candle)
                    if len(candle_windows[sym]) > max_window:
                        candle_windows[sym] = candle_windows[sym][-max_window:]

            # Keep BTC window for market context
            btc_candle = candle_index.get("BTC", {}).get(ts)
            if btc_candle:
                btc_candles_window.append(btc_candle)
                if len(btc_candles_window) > max_window:
                    btc_candles_window = btc_candles_window[-max_window:]

            # Build market context
            phase = _derive_market_phase(btc_candles_window)
            fgi = _derive_fear_greed(btc_candles_window)
            ctx = MarketContext(
                phase=phase, btc_dominance=48.0, fear_greed_index=fgi,
                total_market_cap_change_d1=0.0, timestamp=now_ms,
            )

            # Update existing positions
            for sym in symbol_candles:
                candle = candle_index[sym].get(ts)
                if candle:
                    # Filter positions for this symbol
                    sym_positions = [p for p in sim.open_positions if p.symbol == sym]
                    other_positions = [p for p in sim.open_positions if p.symbol != sym]
                    sim.open_positions = sym_positions
                    sim.update_positions(candle, now_ms, candle_windows[sym],
                                        config, self.verbose)
                    sim.open_positions = sim.open_positions + other_positions

            # Scan for new signals
            for sym in symbol_candles:
                window = candle_windows[sym]
                if len(window) < sim.required_candles():
                    continue
                if any(p.symbol == sym for p in sim.open_positions):
                    continue

                # Strategy-specific scan with extra data
                signal = None
                if strategy_id == "correlation_break":
                    signal = sim.scan(sym, window, config, ctx, now_ms,
                                      btc_candles=btc_candles_window)
                elif strategy_id == "narrative_momentum":
                    signal = sim.scan(sym, window, config, ctx, now_ms,
                                      all_candle_windows=candle_windows)
                else:
                    signal = sim.scan(sym, window, config, ctx, now_ms)

                if signal:
                    pos = sim.open_position(signal, now_ms, config, window, ctx)
                    if pos:
                        trade_count += 1
                        if self.verbose:
                            print(f"  OPEN {sym} {signal.side} {strategy_id} "
                                  f"score={signal.score:.0f} @ ${signal.entry_price:.2f}")

            # Track equity and drawdown after all symbols processed this tick
            open_pnl = 0.0
            for pos in sim.open_positions:
                if pos.side == "long":
                    open_pnl += pos.size_usd * ((pos.current_price - pos.entry_price) / pos.entry_price)
                else:
                    open_pnl += pos.size_usd * ((pos.entry_price - pos.current_price) / pos.entry_price)
            equity = sim.balance + sum(p.size_usd for p in sim.open_positions) + open_pnl
            if equity > sim.peak_equity:
                sim.peak_equity = equity
            if sim.peak_equity > 0:
                dd = (sim.peak_equity - equity) / sim.peak_equity
                sim.max_dd = max(sim.max_dd, dd)

        # Force close remaining
        sim.force_close_all(candle_windows, float(timeline[-1]) if timeline else self.end_ms)

        result = sim.get_result(config)
        return result

    def run_all(self) -> list[StrategyBacktestResult]:
        """Run all strategies sequentially."""
        symbol_candles = self.load_data()
        if not symbol_candles:
            print("ERROR: No data loaded. Check your internet connection.")
            return []

        # Use a fresh config copy for each strategy (isolate parameter changes)
        strategies = list(ALL_SIMULATORS.keys())

        print("=" * 80)
        print(f"SYSTEMATIC BACKTEST: {len(strategies)} strategies, "
              f"{len(symbol_candles)} symbols, {self.start_date} to {self.end_date}")
        print("=" * 80)

        for i, strategy_id in enumerate(strategies):
            config = copy.deepcopy(default_scanner_config)
            print(f"\n[{i+1}/{len(strategies)}] Testing: {strategy_id}")
            print("-" * 60)

            result = self.run_strategy(strategy_id, symbol_candles, config)
            self.results.append(result)

            # Print summary
            if result.total_trades == 0:
                print(f"  No trades generated")
            else:
                print(f"  Trades: {result.total_trades} | "
                      f"W/L: {result.wins}/{result.losses} | "
                      f"Win rate: {result.win_rate*100:.1f}% | "
                      f"PnL: ${result.total_pnl_usd:.2f} | "
                      f"Max DD: {result.max_drawdown_pct*100:.1f}%")

            if result.loss_analyses:
                # Group by category
                cats: dict[str, int] = {}
                for la in result.loss_analyses:
                    cats[la.category] = cats.get(la.category, 0) + 1
                print(f"  Loss breakdown: {dict(sorted(cats.items(), key=lambda x: -x[1]))}")

            if result.parameter_changes:
                print(f"  Parameter changes: {len(result.parameter_changes)}")
                for pc in result.parameter_changes[:3]:
                    print(f"    {pc['key']}: {pc['old']:.4f} -> {pc['new']:.4f} ({pc['reason']})")
                if len(result.parameter_changes) > 3:
                    print(f"    ... and {len(result.parameter_changes) - 3} more")

            if result.issues_found:
                print(f"  Issues: {result.issues_found}")

        # Save report
        self.save_report()
        self.print_summary()

        return self.results

    def save_report(self):
        """Save detailed JSON report."""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = REPORT_DIR / f"backtest_{timestamp}.json"

        report = {
            "timestamp": timestamp,
            "config": {
                "symbols": self.symbols,
                "start_date": self.start_date,
                "end_date": self.end_date,
            },
            "strategies": [],
        }

        for r in self.results:
            strategy_data = {
                "strategy": r.strategy,
                "total_trades": r.total_trades,
                "wins": r.wins,
                "losses": r.losses,
                "win_rate": round(r.win_rate, 4),
                "total_pnl_usd": round(r.total_pnl_usd, 2),
                "max_drawdown_pct": round(r.max_drawdown_pct, 4),
                "avg_hold_hours": round(r.avg_hold_hours, 2),
                "data_quality_score": round(r.data_quality_score, 1),
                "issues_found": r.issues_found,
                "parameter_changes": r.parameter_changes,
                "loss_analyses": [la.to_dict() for la in r.loss_analyses],
            }
            report["strategies"].append(strategy_data)

        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nDetailed report saved to: {report_path}")

    def print_summary(self):
        """Print final summary across all strategies."""
        print("\n" + "=" * 80)
        print("BACKTEST SUMMARY")
        print("=" * 80)

        # Strategy ranking table
        active = [r for r in self.results if r.total_trades > 0]
        skipped = [r for r in self.results if r.total_trades == 0]

        if active:
            active.sort(key=lambda r: r.total_pnl_usd, reverse=True)
            print(f"\n{'Strategy':<30} {'Trades':>6} {'Win%':>6} {'PnL':>10} {'MaxDD':>8} {'Data':>6} {'Issues':>7}")
            print("-" * 80)
            for r in active:
                print(f"{r.strategy:<30} {r.total_trades:>6} {r.win_rate*100:>5.1f}% "
                      f"{'${:,.0f}'.format(r.total_pnl_usd):>10} {r.max_drawdown_pct*100:>7.1f}% "
                      f"{r.data_quality_score:>5.0f}% {len(r.issues_found):>7}")

        if skipped:
            print(f"\nSkipped ({len(skipped)}):")
            for r in skipped:
                reasons = r.issues_found[0] if r.issues_found else "no trades generated"
                print(f"  {r.strategy}: {reasons}")

        # Top issues across all strategies
        all_issues: dict[str, int] = {}
        for r in self.results:
            for la in r.loss_analyses:
                key = f"{la.strategy}:{la.loss_reason}"
                all_issues[key] = all_issues.get(key, 0) + 1

        if all_issues:
            print(f"\nTop recurring loss reasons:")
            for issue, count in sorted(all_issues.items(), key=lambda x: -x[1])[:10]:
                print(f"  {issue}: {count} occurrences")

        # All parameter changes
        all_changes: list[dict] = []
        for r in self.results:
            all_changes.extend(r.parameter_changes)
        if all_changes:
            # Group by parameter
            param_changes: dict[str, list] = {}
            for pc in all_changes:
                param_changes.setdefault(pc["key"], []).append(pc)
            print(f"\nParameter adjustments summary:")
            for key, changes in sorted(param_changes.items()):
                first_old = changes[0]["old"]
                last_new = changes[-1]["new"]
                print(f"  {key}: {first_old:.4f} -> {last_new:.4f} ({len(changes)} adjustments)")

        # CAGR calculation
        try:
            start_dt = datetime.datetime.strptime(self.start_date, "%Y-%m-%d")
            end_dt = datetime.datetime.strptime(self.end_date, "%Y-%m-%d")
            years = max(0.1, (end_dt - start_dt).days / 365.25)
        except Exception:
            years = 1.0

        # Per-strategy CAGR
        print(f"\n{'='*80}")
        print(f"PER-STRATEGY CAGR ({years:.1f} years, {self.start_date} to {self.end_date})")
        print(f"{'='*80}")
        print(f"{'Strategy':35s} {'Trades':>7s} {'Win%':>6s} {'Final Balance':>15s} {'Total Return':>13s} {'CAGR':>10s}")
        print(f"{'-'*86}")

        for r in sorted(active, key=lambda x: -x.total_pnl_usd):
            final = DEFAULT_BALANCE + r.total_pnl_usd
            total_return_pct = (r.total_pnl_usd / DEFAULT_BALANCE) * 100
            if final > 0 and final > DEFAULT_BALANCE:
                cagr = ((final / DEFAULT_BALANCE) ** (1 / years) - 1) * 100
                cagr_str = f"{cagr:,.1f}%"
            elif final > 0:
                cagr = -((DEFAULT_BALANCE / final) ** (1 / years) - 1) * 100
                cagr_str = f"{cagr:,.1f}%"
            else:
                cagr_str = "BANKRUPT"
            wr = (r.wins / r.total_trades * 100) if r.total_trades > 0 else 0
            print(f"{r.strategy:35s} {r.total_trades:7d} {wr:5.1f}% ${final:>14,.2f} {total_return_pct:>+12,.1f}% {cagr_str:>10s}")

        # Overall
        total_trades = sum(r.total_trades for r in self.results)
        total_pnl = sum(r.total_pnl_usd for r in self.results)
        total_wins = sum(r.wins for r in self.results)
        overall_final = DEFAULT_BALANCE + total_pnl
        overall_return = (total_pnl / DEFAULT_BALANCE) * 100

        print(f"{'-'*86}")
        print(f"{'OVERALL (sum of isolated)':35s} {total_trades:7d} {total_wins/max(1,total_trades)*100:5.1f}% ${overall_final:>14,.2f} {overall_return:>+12,.1f}%")

        if overall_final > DEFAULT_BALANCE:
            overall_cagr = ((overall_final / DEFAULT_BALANCE) ** (1 / years) - 1) * 100
            print(f"\nOverall CAGR: {overall_cagr:,.1f}% over {years:.1f} years")
        else:
            print(f"\nOverall CAGR: negative over {years:.1f} years")

        print(f"Starting balance: ${DEFAULT_BALANCE:,.0f} per strategy (isolated mode)")
        print(f"Symbols loaded: {len([s for s in self.symbols])}")


# ---------------------------------------------------------------------------
# NEW: Trend Following Strategy — captures multi-week/month trends
# ---------------------------------------------------------------------------

class TrendFollowingSimulator(StrategySimulator):
    """Donchian Channel breakout — Turtle Trading adapted for crypto.
    Research: 15-day (360 1h-bar) Donchian breakout = 145% CAGR on BTC.
    Uses long-term SMA filter and ATR-based position sizing."""
    strategy_id = "trend_following"
    tier = "swing"

    _DONCHIAN_LOOKBACK = 15    # 15 days (daily candles)
    _EXIT_LOOKBACK = 8         # 8 day exit channel
    _TREND_FILTER = 50         # 50-day SMA

    def required_candles(self):
        return max(self._DONCHIAN_LOOKBACK + 5, self._TREND_FILTER + 5)

    def timeframe(self):
        return "1d"

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < self._DONCHIAN_LOOKBACK + 2:
            return None

        price = candles[-1]["close"]
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        # Donchian Channel: highest high / lowest low over lookback
        dc_high = max(highs[-self._DONCHIAN_LOOKBACK - 1:-1])  # exclude current bar
        dc_low = min(lows[-self._DONCHIAN_LOOKBACK - 1:-1])

        # Long-term trend filter: 50-day SMA direction
        if len(closes) >= self._TREND_FILTER:
            sma_now = sum(closes[-self._TREND_FILTER:]) / self._TREND_FILTER
            sma_prev = sum(closes[-self._TREND_FILTER - 24:-24]) / self._TREND_FILTER if len(closes) > self._TREND_FILTER + 24 else sma_now
        else:
            sma_now = sum(closes[-200:]) / min(200, len(closes))
            sma_prev = sma_now

        # ATR for stops
        atr = _compute_atr_from_candles(candles)
        atr_pct = (atr / price) if atr and price > 0 else 0.04
        stop_dist = max(0.04, min(0.10, atr_pct * 3.0))

        # Volume confirmation
        vol_ratio = _compute_volume_ratio(candles, 20)

        # LONG: price breaks above Donchian high + trend filter up
        if (price > dc_high and sma_now > sma_prev
                and vol_ratio > 1.0
                and ctx.phase not in ("extreme_fear",)):
            score = min(88, 60 + min(20, (price - dc_high) / dc_high * 500) + vol_ratio * 2)
            self._set_cooldown(symbol, now_ms, 86_400_000 * 3)  # 3-day cooldown
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="trend_following", side="long", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} Donchian {self._DONCHIAN_LOOKBACK}-bar high break, vol={vol_ratio:.1f}x",
                entry_price=price, stop_price=price * (1 - stop_dist),
                target_price=price * (1 + stop_dist * 3.0),  # 3:1 R:R for trend trades
                suggested_size_usd=120, expires_at=now_ms + 14 * 86_400_000, created_at=now_ms,
            )

        # SHORT: price breaks below Donchian low + trend filter down
        if (price < dc_low and sma_now < sma_prev
                and vol_ratio > 1.0
                and ctx.phase not in ("extreme_greed",)):
            score = min(85, 58 + min(20, (dc_low - price) / price * 500) + vol_ratio * 2)
            self._set_cooldown(symbol, now_ms, 86_400_000 * 3)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="trend_following", side="short", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} Donchian low break, trend down",
                entry_price=price, stop_price=price * (1 + stop_dist),
                target_price=price * (1 - stop_dist * 3.0),
                suggested_size_usd=100, expires_at=now_ms + 14 * 86_400_000, created_at=now_ms,
            )
        return None


# ---------------------------------------------------------------------------
# NEW: RSI Divergence Strategy — hidden bullish/bearish divergences
# ---------------------------------------------------------------------------

class RSIDivergenceSimulator(StrategySimulator):
    """RSI divergence with confirmation candle, tighter thresholds, longer lookback.
    Research: RSI <30/>70 + confirmation candle + ADX<30 trend filter = 50-55% WR."""
    strategy_id = "rsi_divergence"
    tier = "swing"

    def __init__(self):
        super().__init__()
        self._price_rsi_history: dict[str, list[tuple[float, float]]] = {}
        self._pending_divergence: dict[str, dict] = {}  # symbol -> {side, candles_waiting}

    def required_candles(self):
        return 40

    def timeframe(self):
        return "1d"

    def timeframe(self):
        return "4h"

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 35:
            return None

        price = candles[-1]["close"]
        rsi = _compute_rsi_from_candles(candles)
        if rsi is None:
            return None

        # Track price/RSI pairs
        hist = self._price_rsi_history.setdefault(symbol, [])
        hist.append((price, rsi))
        if len(hist) > 100:
            self._price_rsi_history[symbol] = hist[-100:]
        if len(hist) < 30:
            return None

        # ADX filter REMOVED — research shows it blocks too many valid signals
        # Instead, we rely on RSI thresholds + confirmation candle

        # Check for confirmation of a pending divergence
        pending = self._pending_divergence.get(symbol)
        if pending:
            pending["candles_waiting"] += 1
            if pending["candles_waiting"] > 3:
                self._pending_divergence.pop(symbol, None)  # expired
            elif pending["side"] == "long":
                # Confirmation: bullish candle (close > open) shows buying pressure
                if candles[-1]["close"] > candles[-1]["open"]:
                    atr = _compute_atr_from_candles(candles)
                    atr_pct = (atr / price) if atr and price > 0 else 0.04
                    stop_dist = max(0.05, min(0.10, atr_pct * 3.0))  # wide stops
                    self._pending_divergence.pop(symbol)
                    self._set_cooldown(symbol, now_ms, 86_400_000)
                    return TradeSignal(
                        id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                        strategy="rsi_divergence", side="long", tier="swing",
                        score=pending["score"], confidence="medium",
                        sources=["price_action"],
                        reasoning=pending["reasoning"] + " — CONFIRMED",
                        entry_price=price, stop_price=price * (1 - stop_dist),
                        target_price=price * (1 + stop_dist * 3.0),  # 3:1 R:R
                        suggested_size_usd=80, expires_at=now_ms + 7_200_000, created_at=now_ms,
                    )
            elif pending["side"] == "short":
                # Confirmation: bearish candle (close < open) shows selling pressure
                if candles[-1]["close"] < candles[-1]["open"]:
                    atr = _compute_atr_from_candles(candles)
                    atr_pct = (atr / price) if atr and price > 0 else 0.04
                    stop_dist = max(0.05, min(0.10, atr_pct * 3.0))  # wide stops
                    self._pending_divergence.pop(symbol)
                    self._set_cooldown(symbol, now_ms, 86_400_000)
                    return TradeSignal(
                        id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                        strategy="rsi_divergence", side="short", tier="swing",
                        score=pending["score"], confidence="medium",
                        sources=["price_action"],
                        reasoning=pending["reasoning"] + " — CONFIRMED",
                        entry_price=price, stop_price=price * (1 + stop_dist),
                        target_price=price * (1 - stop_dist * 3.0),
                        suggested_size_usd=70, expires_at=now_ms + 7_200_000, created_at=now_ms,
                    )
            return None

        # Extended lookback: compare last 8 candles vs earlier 15 candles (was 5 vs 10)
        lookback = hist[-25:]
        recent = hist[-8:]
        earlier = lookback[:15]
        if not earlier:
            return None

        rsi_at_min_price_recent = min(recent, key=lambda r: r[0])[1]
        rsi_at_min_price_earlier = min(earlier, key=lambda r: r[0])[1]
        min_price_recent = min(r[0] for r in recent)
        min_price_earlier = min(r[0] for r in earlier)

        rsi_at_max_price_recent = max(recent, key=lambda r: r[0])[1]
        rsi_at_max_price_earlier = max(earlier, key=lambda r: r[0])[1]
        max_price_recent = max(r[0] for r in recent)
        max_price_earlier = max(r[0] for r in earlier)

        # BULLISH DIVERGENCE: price lower low, RSI higher low
        # Loosened: RSI < 40 (was 35), divergence >= 3 points (was 5)
        if (min_price_recent < min_price_earlier * 0.995
                and rsi_at_min_price_recent > rsi_at_min_price_earlier + 3
                and rsi < 40
                and ctx.phase not in ("extreme_fear",)):
            div_strength = rsi_at_min_price_recent - rsi_at_min_price_earlier
            score = min(85, 60 + div_strength * 2)
            self._pending_divergence[symbol] = {
                "side": "long", "candles_waiting": 0, "score": score,
                "reasoning": f"{symbol} bullish RSI div (price new low, RSI +{div_strength:.0f})"
            }
            return None  # wait for confirmation

        # BEARISH DIVERGENCE: price higher high, RSI lower high
        # Loosened: RSI > 60 (was 65), divergence >= 3 (was 5)
        if (max_price_recent > max_price_earlier * 1.005
                and rsi_at_max_price_recent < rsi_at_max_price_earlier - 3
                and rsi > 60
                and ctx.phase not in ("extreme_greed",)):
            div_strength = rsi_at_max_price_earlier - rsi_at_max_price_recent
            score = min(82, 58 + div_strength * 2)
            self._pending_divergence[symbol] = {
                "side": "short", "candles_waiting": 0, "score": score,
                "reasoning": f"{symbol} bearish RSI div (price new high, RSI -{div_strength:.0f})"
            }
            return None  # wait for confirmation

        return None


# ---------------------------------------------------------------------------
# NEW: Volume Profile Breakout — breakout from consolidation with volume
# ---------------------------------------------------------------------------

class VolumeBreakoutSimulator(StrategySimulator):
    """Detects consolidation ranges and trades breakouts with volume confirmation."""
    strategy_id = "volume_breakout"
    tier = "swing"

    def required_candles(self):
        return 50

    def timeframe(self):
        return "4h"

    def scan(self, symbol, candles, config, ctx, now_ms):
        if self._check_cooldown(symbol, now_ms):
            return None
        if len(candles) < 40:
            return None

        price = candles[-1]["close"]
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        # Define consolidation range from last 20 candles (excluding last 2)
        range_candles = candles[-22:-2]
        range_high = max(c["high"] for c in range_candles)
        range_low = min(c["low"] for c in range_candles)
        range_width = (range_high - range_low) / range_low if range_low > 0 else 0

        # Range must be tight enough to be a real consolidation (< 8%)
        if range_width > 0.08 or range_width < 0.01:
            return None

        # Volume confirmation: current candle volume vs average
        vol_ratio = _compute_volume_ratio(candles, 20)
        if vol_ratio < 2.0:
            return None  # need strong volume for breakout

        atr = _compute_atr_from_candles(candles)
        atr_pct = (atr / price) if atr and price > 0 else 0.03
        stop_dist = max(0.025, min(0.05, atr_pct * 1.5))

        # Bullish breakout: price closes above range high
        if price > range_high * 1.005 and ctx.phase not in ("extreme_fear", "bear"):
            breakout_pct = (price - range_high) / range_high
            score = min(88, 60 + vol_ratio * 3 + breakout_pct * 200)
            self._set_cooldown(symbol, now_ms, 86_400_000)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="volume_breakout", side="long", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} breakout above ${range_high:.2f} range ({range_width*100:.1f}% range, {vol_ratio:.1f}x vol)",
                entry_price=price, stop_price=price * (1 - stop_dist),
                target_price=price * (1 + stop_dist * 3.0),  # 3:1 R:R
                suggested_size_usd=90, expires_at=now_ms + 7_200_000, created_at=now_ms,
            )

        # Bearish breakdown: price closes below range low
        if price < range_low * 0.995 and ctx.phase not in ("extreme_greed", "bull"):
            breakdown_pct = (range_low - price) / range_low
            score = min(85, 58 + vol_ratio * 3 + breakdown_pct * 200)
            self._set_cooldown(symbol, now_ms, 86_400_000)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="volume_breakout", side="short", tier="swing",
                score=score, confidence="medium",
                sources=["price_action"],
                reasoning=f"{symbol} breakdown below ${range_low:.2f} range ({range_width*100:.1f}% range, {vol_ratio:.1f}x vol)",
                entry_price=price, stop_price=price * (1 + stop_dist),
                target_price=price * (1 - stop_dist * 3.0),
                suggested_size_usd=80, expires_at=now_ms + 7_200_000, created_at=now_ms,
            )
        return None


# Re-engineered strategies — re-enabled with research-backed fixes
ALL_SIMULATORS["ema_crossover"] = EMACrossoverSimulator
ALL_SIMULATORS["bb_squeeze"] = BBSqueezeSimulator
ALL_SIMULATORS["rsi_divergence"] = RSIDivergenceSimulator
ALL_SIMULATORS["trend_following"] = TrendFollowingSimulator
ALL_SIMULATORS["volume_breakout"] = VolumeBreakoutSimulator


# ---------------------------------------------------------------------------
# Combined Portfolio Backtest — all strategies share ONE balance
# ---------------------------------------------------------------------------

class CombinedBacktester:
    """Runs ALL strategies simultaneously from a single shared balance.
    This reflects production behavior and enables proper compounding."""

    def __init__(self, symbols: list[str], start_date: str, end_date: str,
                 initial_balance: float = 10_000.0, verbose: bool = False):
        self.symbols = symbols
        self.start_date = start_date
        self.end_date = end_date
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.verbose = verbose
        self.start_ms = _date_to_ms(start_date)
        self.end_ms = _date_to_ms(end_date)
        self.open_positions: list[BacktestPosition] = []
        self.closed_positions: list[BacktestPosition] = []
        self.peak_equity = initial_balance
        self.max_dd = 0.0
        self.equity_curve: list[tuple[float, float]] = []  # (timestamp, equity)
        self.strategy_pnl: dict[str, float] = {}

    def _kelly_size(self, qual_score: float, strategy: str) -> float:
        if self.balance < 100:
            return 0
        fraction = 0.25  # 25% of balance — optimal balance of growth vs drawdown
        qual_mult = 0.5 + (qual_score / 100)
        raw = fraction * self.balance * qual_mult
        # Cap at 40% of balance per position
        max_position = self.balance * 0.40
        return max(10, min(raw, max_position))

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        if (side == "long" and is_entry) or (side == "short" and not is_entry):
            return price * (1 + SLIPPAGE_PCT)
        return price * (1 - SLIPPAGE_PCT)

    def run(self) -> dict:
        print(f"\nLoading kline data for {len(self.symbols)} symbols "
              f"({self.start_date} to {self.end_date})...")
        symbol_candles: dict[str, list[dict]] = {}
        for symbol in self.symbols:
            print(f"  Loading {symbol}...", end=" ", flush=True)
            try:
                candles = load_klines(symbol, "1h", self.start_ms, self.end_ms)
                if candles:
                    symbol_candles[symbol] = candles
                    print(f"{len(candles)} candles")
                else:
                    print("NO DATA")
            except Exception as e:
                print(f"ERROR: {e}")
        print(f"  Loaded data for {len(symbol_candles)}/{len(self.symbols)} symbols\n")

        # Load external data feeds for strategies that need them
        symbol_funding: dict[str, list[dict]] = {}
        symbol_oi: dict[str, list[dict]] = {}
        symbol_futures: dict[str, list[dict]] = {}

        # Check which data feeds are needed by active simulators
        needs_funding = any(
            "funding" in cls().data_feeds()
            for cls in ALL_SIMULATORS.values()
        )
        needs_oi = any(
            "oi" in cls().data_feeds()
            for cls in ALL_SIMULATORS.values()
        )
        needs_futures = any(
            "futures_klines" in cls().data_feeds()
            for cls in ALL_SIMULATORS.values()
        )

        if needs_funding:
            print("Loading funding rate data...")
            for symbol in symbol_candles:
                print(f"  Funding {symbol}...", end=" ", flush=True)
                try:
                    data = load_funding_rates(symbol, self.start_ms, self.end_ms)
                    if data:
                        symbol_funding[symbol] = data
                        print(f"{len(data)} records")
                    else:
                        print("NO DATA")
                except Exception as e:
                    print(f"ERROR: {e}")
            print(f"  Loaded funding for {len(symbol_funding)}/{len(symbol_candles)} symbols\n")

        if needs_oi:
            print("Loading open interest data...")
            for symbol in symbol_candles:
                print(f"  OI {symbol}...", end=" ", flush=True)
                try:
                    data = load_open_interest(symbol, self.start_ms, self.end_ms)
                    if data:
                        symbol_oi[symbol] = data
                        print(f"{len(data)} records")
                    else:
                        print("NO DATA")
                except Exception as e:
                    print(f"ERROR: {e}")
            print(f"  Loaded OI for {len(symbol_oi)}/{len(symbol_candles)} symbols\n")

        if needs_futures:
            print("Loading futures kline data...")
            for symbol in symbol_candles:
                print(f"  Futures {symbol}...", end=" ", flush=True)
                try:
                    data = load_futures_klines(symbol, "1h", self.start_ms, self.end_ms)
                    if data:
                        symbol_futures[symbol] = data
                        print(f"{len(data)} candles")
                    else:
                        print("NO DATA")
                except Exception as e:
                    print(f"ERROR: {e}")
            print(f"  Loaded futures for {len(symbol_futures)}/{len(symbol_candles)} symbols\n")

        if not symbol_candles:
            print("ERROR: No data loaded.")
            return {}

        # Use all strategies from the registry
        simulators: dict[str, StrategySimulator] = {}
        for sid, cls in ALL_SIMULATORS.items():
            sim = cls()
            # Inject external data into simulators that need it
            if hasattr(sim, "funding_data") and symbol_funding:
                sim.funding_data = symbol_funding
            if hasattr(sim, "oi_data") and symbol_oi:
                sim.oi_data = symbol_oi
            if hasattr(sim, "futures_candles") and symbol_futures:
                sim.futures_candles = symbol_futures
            simulators[sid] = sim

        config = copy.deepcopy(default_scanner_config)
        # Build unified timeline
        all_timestamps: set[int] = set()
        for candles in symbol_candles.values():
            for c in candles:
                all_timestamps.add(c["open_time"])
        timeline = sorted(all_timestamps)

        # Index candles
        candle_index: dict[str, dict[int, dict]] = {}
        for sym, candles in symbol_candles.items():
            candle_index[sym] = {c["open_time"]: c for c in candles}

        # Rolling windows
        candle_windows: dict[str, list[dict]] = {s: [] for s in symbol_candles}
        max_window = 80
        btc_candles_window: list[dict] = []

        total_trades = 0
        yearly_pnl: dict[str, float] = {}

        print("=" * 80)
        print(f"COMBINED PORTFOLIO BACKTEST: {len(simulators)} strategies, "
              f"{len(symbol_candles)} symbols, {self.start_date} to {self.end_date}")
        print(f"Starting balance: ${self.initial_balance:,.0f}")
        print("=" * 80)

        for ts_idx, ts in enumerate(timeline):
            now_ms = float(ts)

            # Update candle windows
            for sym in symbol_candles:
                candle = candle_index[sym].get(ts)
                if candle:
                    candle_windows[sym].append(candle)
                    if len(candle_windows[sym]) > max_window:
                        candle_windows[sym] = candle_windows[sym][-max_window:]

            btc_candle = candle_index.get("BTC", {}).get(ts)
            if btc_candle:
                btc_candles_window.append(btc_candle)
                if len(btc_candles_window) > max_window:
                    btc_candles_window = btc_candles_window[-max_window:]

            # Market context
            phase = _derive_market_phase(btc_candles_window)
            fgi = _derive_fear_greed(btc_candles_window)
            ctx = MarketContext(
                phase=phase, btc_dominance=48.0, fear_greed_index=fgi,
                total_market_cap_change_d1=0.0, timestamp=now_ms,
            )

            # Update ALL open positions
            closed_this_tick: list[BacktestPosition] = []
            still_open: list[BacktestPosition] = []

            for pos in self.open_positions:
                candle = candle_index.get(pos.symbol, {}).get(ts)
                if not candle:
                    still_open.append(pos)
                    continue

                high, low, close = candle["high"], candle["low"], candle["close"]
                pos.high_watermark = max(pos.high_watermark, high)
                pos.low_watermark = min(pos.low_watermark, low)
                pos.current_price = close

                if pos.side == "long":
                    pos.mfe_pct = max(pos.mfe_pct, (pos.high_watermark - pos.entry_price) / pos.entry_price)
                    pos.mae_pct = min(pos.mae_pct, (pos.low_watermark - pos.entry_price) / pos.entry_price)
                else:
                    pos.mfe_pct = max(pos.mfe_pct, (pos.entry_price - pos.low_watermark) / pos.entry_price)
                    pos.mae_pct = min(pos.mae_pct, (pos.entry_price - pos.high_watermark) / pos.entry_price)

                # Update trailing stop — only after meaningful profit
                min_profit_to_trail = pos.trail_pct * 0.5
                if pos.side == "long":
                    current_profit = (pos.high_watermark - pos.entry_price) / pos.entry_price
                    if current_profit >= min_profit_to_trail:
                        new_stop = pos.high_watermark * (1 - pos.trail_pct)
                        if new_stop > pos.stop_price:
                            pos.stop_price = new_stop
                else:
                    current_profit = (pos.entry_price - pos.low_watermark) / pos.entry_price
                    if current_profit >= min_profit_to_trail:
                        new_stop = pos.low_watermark * (1 + pos.trail_pct)
                        if new_stop < pos.stop_price:
                            pos.stop_price = new_stop

                # Check exits — TP first
                exit_reason = None
                exit_price = close

                if pos.target_price:
                    if pos.side == "long" and high >= pos.target_price:
                        exit_reason = "take_profit"
                        exit_price = pos.target_price
                    elif pos.side == "short" and low <= pos.target_price:
                        exit_reason = "take_profit"
                        exit_price = pos.target_price

                if exit_reason is None:
                    if pos.side == "long" and low <= pos.stop_price:
                        exit_reason = "trailing_stop"
                        exit_price = pos.stop_price
                    elif pos.side == "short" and high >= pos.stop_price:
                        exit_reason = "trailing_stop"
                        exit_price = pos.stop_price

                if exit_reason is None and (now_ms - pos.opened_at) >= pos.max_hold_ms:
                    exit_reason = "time_limit"
                    exit_price = close

                if exit_reason:
                    exit_price = self._apply_slippage(exit_price, pos.side, False)
                    commission = pos.size_usd * COMMISSION_PCT

                    if pos.side == "long":
                        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
                    else:
                        pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

                    pnl_usd = pos.size_usd * pnl_pct - commission
                    pos.exit_price = exit_price
                    pos.closed_at = now_ms
                    pos.pnl_pct = pnl_pct
                    pos.pnl_usd = pnl_usd
                    pos.exit_reason = exit_reason

                    self.balance += pos.size_usd + pnl_usd
                    self.closed_positions.append(pos)
                    closed_this_tick.append(pos)
                    self.strategy_pnl[pos.strategy] = self.strategy_pnl.get(pos.strategy, 0) + pnl_usd

                    # Track yearly PnL
                    year = datetime.datetime.utcfromtimestamp(now_ms / 1000).strftime("%Y")
                    yearly_pnl[year] = yearly_pnl.get(year, 0) + pnl_usd

                    if self.verbose:
                        print(f"  {'WIN' if pnl_usd > 0 else 'LOSS'} {pos.symbol} {pos.strategy} "
                              f"{pos.side} {pnl_pct*100:+.2f}% ${pnl_usd:+.1f} [{exit_reason}]")
                else:
                    still_open.append(pos)

            self.open_positions = still_open

            # Scan for new signals across ALL strategies
            if self.balance < 100:
                continue  # Skip scanning when bankrupt
            for sid, sim in simulators.items():
                for sym in symbol_candles:
                    window = candle_windows[sym]
                    if len(window) < sim.required_candles():
                        continue
                    # Max 8 total open positions to manage risk
                    if len(self.open_positions) >= 8:
                        break
                    # One position per symbol across all strategies
                    if any(p.symbol == sym for p in self.open_positions):
                        continue
                    # Balance check
                    if self.balance < 100:
                        break

                    # Scan
                    signal = None
                    if sid == "correlation_break":
                        signal = sim.scan(sym, window, config, ctx, now_ms,
                                          btc_candles=btc_candles_window)
                    elif sid == "correlation_break_eth":
                        eth_window = candle_windows.get("ETH", [])
                        if len(eth_window) >= sim.required_candles():
                            signal = sim.scan(sym, window, config, ctx, now_ms,
                                              eth_candles=eth_window)
                    elif sid == "narrative_momentum":
                        signal = sim.scan(sym, window, config, ctx, now_ms,
                                          all_candle_windows=candle_windows)
                    else:
                        signal = sim.scan(sym, window, config, ctx, now_ms)

                    if signal:
                        size_usd = self._kelly_size(signal.score, sid)
                        entry_price = self._apply_slippage(signal.entry_price, signal.side, True)
                        commission = size_usd * COMMISSION_PCT

                        if size_usd + commission > self.balance:
                            continue

                        self.balance -= (size_usd + commission)  # Reserve capital for position

                        # Derive trail_pct from signal stop
                        if signal.stop_price and signal.stop_price > 0:
                            stop_price = signal.stop_price
                            if signal.side == "long":
                                trail_pct = max(0.005, (entry_price - stop_price) / entry_price)
                            else:
                                trail_pct = max(0.005, (stop_price - entry_price) / entry_price)
                        else:
                            trail_pct = 0.05
                            stop_price = (entry_price * (1 - trail_pct) if signal.side == "long"
                                          else entry_price * (1 + trail_pct))

                        max_hold = (config.max_hold_ms_scalp if signal.tier == "scalp"
                                    else config.max_hold_ms_swing)

                        pos = BacktestPosition(
                            id=str(uuid.uuid4()), symbol=signal.symbol,
                            strategy=signal.strategy, side=signal.side, tier=signal.tier,
                            entry_price=entry_price, size_usd=size_usd,
                            quantity=size_usd / entry_price, opened_at=now_ms,
                            high_watermark=entry_price, low_watermark=entry_price,
                            current_price=entry_price, trail_pct=trail_pct,
                            stop_price=stop_price, max_hold_ms=max_hold,
                            qual_score=signal.score,
                            signal_reasoning=signal.reasoning,
                            target_price=signal.target_price,
                            momentum_at_entry=0.0,
                            market_phase_at_entry=ctx.phase,
                            candle_count_at_entry=len(window),
                        )
                        self.open_positions.append(pos)
                        total_trades += 1

                        if self.verbose:
                            print(f"  OPEN {sym} {signal.side} {sid} score={signal.score:.0f} "
                                  f"@ ${entry_price:.2f} size=${size_usd:.0f}")

            # Track equity
            open_pnl = 0.0
            for pos in self.open_positions:
                if pos.side == "long":
                    open_pnl += pos.size_usd * ((pos.current_price - pos.entry_price) / pos.entry_price)
                else:
                    open_pnl += pos.size_usd * ((pos.entry_price - pos.current_price) / pos.entry_price)
            equity = self.balance + sum(p.size_usd for p in self.open_positions) + open_pnl

            if equity > self.peak_equity:
                self.peak_equity = equity
            if self.peak_equity > 0:
                dd = (self.peak_equity - equity) / self.peak_equity
                self.max_dd = max(self.max_dd, dd)

            # Record equity curve (every 24 hours)
            if ts_idx % 24 == 0:
                self.equity_curve.append((now_ms, equity))

        # Force close remaining
        for pos in self.open_positions:
            candle = None
            if pos.symbol in candle_windows and candle_windows[pos.symbol]:
                last_candle = candle_windows[pos.symbol][-1]
                close = last_candle["close"]
                exit_price = self._apply_slippage(close, pos.side, False)
                commission = pos.size_usd * COMMISSION_PCT

                if pos.side == "long":
                    pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
                else:
                    pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

                pnl_usd = pos.size_usd * pnl_pct - commission
                pos.exit_price = exit_price
                pos.closed_at = self.end_ms
                pos.pnl_pct = pnl_pct
                pos.pnl_usd = pnl_usd
                pos.exit_reason = "force_close"
                self.balance += pos.size_usd + pnl_usd
                self.closed_positions.append(pos)
                self.strategy_pnl[pos.strategy] = self.strategy_pnl.get(pos.strategy, 0) + pnl_usd

        self.open_positions = []

        # Print results
        self._print_results(total_trades, yearly_pnl)
        return {"total_pnl": self.balance - self.initial_balance,
                "return_pct": (self.balance - self.initial_balance) / self.initial_balance * 100}

    def _print_results(self, total_trades: int, yearly_pnl: dict):
        print("\n" + "=" * 80)
        print("COMBINED PORTFOLIO RESULTS")
        print("=" * 80)

        # Per-strategy breakdown
        strategy_trades: dict[str, dict] = {}
        for pos in self.closed_positions:
            st = strategy_trades.setdefault(pos.strategy, {"trades": 0, "wins": 0, "pnl": 0.0})
            st["trades"] += 1
            if pos.pnl_usd and pos.pnl_usd > 0:
                st["wins"] += 1
            st["pnl"] += pos.pnl_usd or 0

        print(f"\n{'Strategy':<30} {'Trades':>6} {'Win%':>6} {'PnL':>12}")
        print("-" * 60)
        for sid in sorted(strategy_trades, key=lambda s: strategy_trades[s]["pnl"], reverse=True):
            st = strategy_trades[sid]
            wr = st["wins"] / st["trades"] if st["trades"] > 0 else 0
            print(f"{sid:<30} {st['trades']:>6} {wr*100:>5.1f}% {'${:,.0f}'.format(st['pnl']):>12}")

        # Yearly breakdown
        print(f"\n{'Year':<10} {'PnL':>12}")
        print("-" * 25)
        for year in sorted(yearly_pnl.keys()):
            print(f"{year:<10} {'${:,.0f}'.format(yearly_pnl[year]):>12}")

        # Overall
        total_pnl = self.balance - self.initial_balance
        total_return = total_pnl / self.initial_balance * 100
        wins = sum(1 for p in self.closed_positions if p.pnl_usd and p.pnl_usd > 0)
        wr = wins / len(self.closed_positions) if self.closed_positions else 0

        print(f"\n{'='*60}")
        print(f"Total trades: {len(self.closed_positions)}")
        print(f"Win rate: {wr*100:.1f}%")
        print(f"Max drawdown: {self.max_dd*100:.1f}%")
        print(f"Starting balance: ${self.initial_balance:,.0f}")
        print(f"Final balance: ${self.balance:,.0f}")
        print(f"Total PnL: ${total_pnl:,.0f} ({total_return:,.1f}%)")

        try:
            start_dt = datetime.datetime.strptime(self.start_date, "%Y-%m-%d")
            end_dt = datetime.datetime.strptime(self.end_date, "%Y-%m-%d")
            years = max(0.1, (end_dt - start_dt).days / 365.25)
            if total_pnl > 0:
                total_ret = self.balance / self.initial_balance
                annualized = (total_ret ** (1 / years) - 1) * 100
                print(f"Annualized return: {annualized:.1f}% over {years:.1f} years")
            else:
                print(f"Annualized return: negative over {years:.1f} years")
        except Exception:
            pass

        # Detailed loss analysis for correlation_break
        cb_trades = [p for p in self.closed_positions if p.strategy == "correlation_break"]
        if cb_trades:
            print(f"\n{'='*60}")
            print("CORRELATION_BREAK DETAILED ANALYSIS")
            print(f"{'='*60}")
            # Long vs short
            longs = [p for p in cb_trades if p.side == "long"]
            shorts = [p for p in cb_trades if p.side == "short"]
            long_wins = sum(1 for p in longs if p.pnl_usd and p.pnl_usd > 0)
            short_wins = sum(1 for p in shorts if p.pnl_usd and p.pnl_usd > 0)
            long_pnl = sum(p.pnl_usd or 0 for p in longs)
            short_pnl = sum(p.pnl_usd or 0 for p in shorts)
            print(f"\nLongs:  {len(longs):>5} trades, WR={long_wins/max(1,len(longs))*100:.1f}%, PnL=${long_pnl:,.0f}")
            print(f"Shorts: {len(shorts):>5} trades, WR={short_wins/max(1,len(shorts))*100:.1f}%, PnL=${short_pnl:,.0f}")

            # By exit reason
            exit_stats: dict[str, dict] = {}
            for p in cb_trades:
                reason = p.exit_reason or "unknown"
                st = exit_stats.setdefault(reason, {"count": 0, "wins": 0, "pnl": 0.0})
                st["count"] += 1
                if p.pnl_usd and p.pnl_usd > 0:
                    st["wins"] += 1
                st["pnl"] += p.pnl_usd or 0
            print(f"\n{'Exit Reason':<20} {'Count':>6} {'Win%':>6} {'PnL':>14}")
            print("-" * 50)
            for reason in sorted(exit_stats, key=lambda r: exit_stats[r]["count"], reverse=True):
                st = exit_stats[reason]
                wr = st["wins"] / st["count"] if st["count"] > 0 else 0
                print(f"{reason:<20} {st['count']:>6} {wr*100:>5.1f}% {'${:,.0f}'.format(st['pnl']):>14}")

            # Average trade duration for wins vs losses
            win_durs = []
            loss_durs = []
            for p in cb_trades:
                dur_h = (p.closed_at - p.opened_at) / 3_600_000 if p.closed_at and p.opened_at else 0
                if p.pnl_usd and p.pnl_usd > 0:
                    win_durs.append(dur_h)
                else:
                    loss_durs.append(dur_h)
            if win_durs:
                print(f"\nAvg win duration:  {sum(win_durs)/len(win_durs):.1f}h")
            if loss_durs:
                print(f"Avg loss duration: {sum(loss_durs)/len(loss_durs):.1f}h")

            # Top 10 losing symbols
            sym_pnl: dict[str, dict] = {}
            for p in cb_trades:
                st = sym_pnl.setdefault(p.symbol, {"trades": 0, "wins": 0, "pnl": 0.0})
                st["trades"] += 1
                if p.pnl_usd and p.pnl_usd > 0:
                    st["wins"] += 1
                st["pnl"] += p.pnl_usd or 0
            worst = sorted(sym_pnl.items(), key=lambda x: x[1]["pnl"])
            print(f"\n{'Top 10 Worst Symbols':<12} {'Trades':>6} {'Win%':>6} {'PnL':>14}")
            print("-" * 42)
            for sym, st in worst[:10]:
                wr = st["wins"] / st["trades"] if st["trades"] > 0 else 0
                print(f"{sym:<12} {st['trades']:>6} {wr*100:>5.1f}% {'${:,.0f}'.format(st['pnl']):>14}")
            print(f"\n{'Top 10 Best Symbols':<12} {'Trades':>6} {'Win%':>6} {'PnL':>14}")
            print("-" * 42)
            for sym, st in reversed(worst[-10:]):
                wr = st["wins"] / st["trades"] if st["trades"] > 0 else 0
                print(f"{sym:<12} {st['trades']:>6} {wr*100:>5.1f}% {'${:,.0f}'.format(st['pnl']):>14}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Systematic strategy backtester")
    parser.add_argument("--symbols", type=str, default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated symbols")
    parser.add_argument("--start", type=str, default=DEFAULT_START, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=DEFAULT_END, help="End date (YYYY-MM-DD)")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Run only this strategy (e.g., momentum_swing)")
    parser.add_argument("--combined", action="store_true",
                        help="Run all strategies from shared balance (production-like)")
    parser.add_argument("--balance", type=float, default=DEFAULT_BALANCE,
                        help="Starting balance (default: $10,000)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print each trade")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    if args.combined:
        bt = CombinedBacktester(symbols, args.start, args.end,
                                initial_balance=args.balance, verbose=args.verbose)
        bt.run()
    elif args.strategy:
        bt = SystematicBacktester(symbols, args.start, args.end, verbose=args.verbose)
        symbol_candles = bt.load_data()
        if symbol_candles:
            config = copy.deepcopy(default_scanner_config)
            result = bt.run_strategy(args.strategy, symbol_candles, config)
            bt.results = [result]
            bt.save_report()
            bt.print_summary()
    else:
        bt = SystematicBacktester(symbols, args.start, args.end, verbose=args.verbose)
        bt.run_all()


if __name__ == "__main__":
    main()
