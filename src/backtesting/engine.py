"""Backtesting engine — simulates trading strategies against historical data."""

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional

from src.types import TradeSignal, Position, ScannerConfig, MarketContext
from src.evaluation.metrics import (
    PortfolioMetrics, StrategyMetrics,
    _mean, _std_dev, _max_drawdown, _max_consecutive_losses, _kelly_fraction,
)
from src.utils.safe_math import safe_ratio
from src.backtesting.data_loader import load_klines


@dataclass
class BacktestConfig:
    symbols: list[str]
    start_date: str  # "2025-01-01"
    end_date: str  # "2025-12-31"
    initial_balance: float = 10000.0
    scanner_config: ScannerConfig = field(default_factory=ScannerConfig)
    commission_pct: float = 0.001  # 0.1% per trade
    slippage_pct: float = 0.0005  # 0.05% slippage
    max_open_positions: int = 5
    interval: str = "1h"  # candle interval for simulation


@dataclass
class BacktestResult:
    metrics: PortfolioMetrics
    total_trades: int
    final_balance: float
    max_drawdown_pct: float
    positions: list[Position]
    equity_curve: list[tuple[int, float]]  # (timestamp_ms, equity)


def _date_to_ms(date_str: str) -> int:
    """Convert 'YYYY-MM-DD' to epoch milliseconds (UTC)."""
    import datetime
    dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Compute RSI from a list of close prices."""
    if len(closes) < period + 1:
        return None
    recent = closes[-(period + 1):]
    gains = losses = 0.0
    for i in range(1, len(recent)):
        diff = recent[i] - recent[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _compute_vwap(candles: list[dict]) -> Optional[float]:
    """Compute VWAP from recent candles."""
    if not candles:
        return None
    sum_pv = sum(c["close"] * c["volume"] for c in candles)
    sum_v = sum(c["volume"] for c in candles)
    return sum_pv / sum_v if sum_v > 0 else None


def _compute_momentum_pct(candles: list[dict], lookback: int) -> Optional[float]:
    """Compute price change percentage over lookback candles."""
    if len(candles) < lookback:
        return None
    old_price = candles[-lookback]["close"]
    new_price = candles[-1]["close"]
    if old_price == 0:
        return None
    return (new_price - old_price) / old_price


def _compute_volume_ratio(candles: list[dict], lookback: int) -> float:
    """Compute current volume vs average volume over lookback."""
    if len(candles) < lookback or lookback == 0:
        return 0
    recent = candles[-lookback:]
    avg_vol = sum(c["volume"] for c in recent) / len(recent)
    current_vol = candles[-1]["volume"]
    return current_vol / avg_vol if avg_vol > 0 else 0


class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.balance = config.initial_balance
        self.open_positions: list[Position] = []
        self.closed_positions: list[Position] = []
        self.equity_curve: list[tuple[int, float]] = []

        # Track cooldowns per symbol-strategy (timestamp when cooldown expires)
        self._cooldowns: dict[str, float] = {}

        # Default strategy stats for Kelly sizing (no historical data in backtest)
        self._default_win_rate = 0.5
        self._default_avg_win = 0.04
        self._default_avg_loss = 0.03

    def _apply_slippage(self, price: float, side: str, entry: bool) -> float:
        """Apply slippage to price. Slippage hurts the trader."""
        slip = self.config.slippage_pct
        if (side == "long" and entry) or (side == "short" and not entry):
            return price * (1 + slip)  # pay more
        return price * (1 - slip)  # receive less

    def _apply_commission(self, size_usd: float) -> float:
        """Return commission cost for a trade."""
        return size_usd * self.config.commission_pct

    def _kelly_size(self, qual_score: float) -> float:
        """Simplified Kelly sizing for backtest (no DB lookups)."""
        b = self._default_avg_win / self._default_avg_loss if self._default_avg_loss > 0 else 1
        p = self._default_win_rate
        q = 1 - p
        raw_kelly = (b * p - q) / b
        if raw_kelly <= 0:
            fraction = 0.01
        else:
            fraction = raw_kelly * 0.25  # quarter Kelly

        qual_multiplier = 0.5 + (qual_score / 100)
        raw_usd = fraction * self.balance * qual_multiplier
        return max(10, min(raw_usd, self.balance * 0.2))  # cap at 20% of balance

    def _check_cooldown(self, symbol: str, strategy: str, now_ms: float) -> bool:
        """Return True if the symbol-strategy is still in cooldown."""
        key = f"{symbol}:{strategy}"
        expiry = self._cooldowns.get(key, 0)
        return now_ms < expiry

    def _set_cooldown(self, symbol: str, strategy: str, now_ms: float, duration_ms: float) -> None:
        key = f"{symbol}:{strategy}"
        self._cooldowns[key] = now_ms + duration_ms

    def _make_market_context(self, candle: dict) -> MarketContext:
        """Build a simplified market context from candle data."""
        return MarketContext(
            phase="neutral",
            btc_dominance=48.0,
            fear_greed_index=50,
            total_market_cap_change_d1=0.0,
            timestamp=float(candle["open_time"]),
        )

    def _scan_momentum(
        self, symbol: str, candles: list[dict], config: ScannerConfig, now_ms: float,
    ) -> Optional[TradeSignal]:
        """Simplified momentum scan for backtesting."""
        if self._check_cooldown(symbol, "momentum_swing", now_ms):
            return None

        # Need enough candles for swing lookback (default 1h = ~1 candle at 1h interval)
        lookback = max(5, min(len(candles), 24))  # up to 24 candles
        mom = _compute_momentum_pct(candles, lookback)
        if mom is None:
            return None

        vol_ratio = _compute_volume_ratio(candles, lookback)

        if mom >= config.momentum_pct_swing and vol_ratio >= config.volume_multiplier_swing:
            score = min(95, 55 + mom * 200)
            if score >= config.min_qual_score_swing:
                price = candles[-1]["close"]
                self._set_cooldown(symbol, "momentum_swing", now_ms, config.cooldown_ms_swing)
                return TradeSignal(
                    id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                    strategy="momentum_swing", side="long", tier="swing",
                    score=score, confidence="high" if score > 75 else "medium",
                    sources=["price_action"],
                    reasoning=f"Backtest: {symbol} +{mom*100:.1f}% momentum with {vol_ratio:.1f}x volume",
                    entry_price=price,
                    stop_price=price * (1 - config.base_trail_pct_swing),
                    suggested_size_usd=100,
                    expires_at=now_ms + 300_000, created_at=now_ms,
                )
        return None

    def _scan_mean_reversion(
        self, symbol: str, candles: list[dict], config: ScannerConfig, now_ms: float,
    ) -> Optional[TradeSignal]:
        """Simplified mean reversion scan for backtesting."""
        if self._check_cooldown(symbol, "mean_reversion", now_ms):
            return None
        if len(candles) < 30:
            return None

        price = candles[-1]["close"]
        vwap = _compute_vwap(candles[-30:])
        closes = [c["close"] for c in candles]
        rsi = _compute_rsi(closes)

        if vwap is None or vwap == 0 or rsi is None:
            return None

        deviation = (price - vwap) / vwap
        vol_ratio = _compute_volume_ratio(candles, 20)

        # Long entry: oversold
        if deviation < -config.vwap_deviation_pct and rsi < config.rsi_oversold and vol_ratio < 1.5:
            dev_score = min(30, abs(deviation) * 500)
            rsi_score = min(20, config.rsi_oversold - rsi)
            score = min(90, 40 + dev_score + rsi_score)
            self._set_cooldown(symbol, "mean_reversion", now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="mean_reversion", side="long", tier="swing",
                score=score, confidence="medium" if score > 70 else "low",
                sources=["price_action"],
                reasoning=f"Backtest: {symbol} {deviation*100:.1f}% below VWAP, RSI={rsi:.0f}",
                entry_price=price, target_price=vwap,
                stop_price=price * 0.98, suggested_size_usd=80,
                expires_at=now_ms + 1_800_000, created_at=now_ms,
            )

        # Short entry: overbought
        if deviation > config.vwap_deviation_pct and rsi > config.rsi_overbought and vol_ratio < 1.5:
            dev_score = min(30, deviation * 500)
            rsi_score = min(20, rsi - config.rsi_overbought)
            score = min(88, 38 + dev_score + rsi_score)
            self._set_cooldown(symbol, "mean_reversion", now_ms, config.cooldown_ms_swing)
            return TradeSignal(
                id=str(uuid.uuid4()), symbol=symbol, product_id=f"{symbol}-USD",
                strategy="mean_reversion", side="short", tier="swing",
                score=score, confidence="medium" if score > 68 else "low",
                sources=["price_action"],
                reasoning=f"Backtest: {symbol} {deviation*100:.1f}% above VWAP, RSI={rsi:.0f}",
                entry_price=price, target_price=vwap,
                stop_price=price * 1.02, suggested_size_usd=60,
                expires_at=now_ms + 1_800_000, created_at=now_ms,
            )

        return None

    def _open_position(self, signal: TradeSignal, now_ms: float) -> Optional[Position]:
        """Open a new position from a signal."""
        if len(self.open_positions) >= self.config.max_open_positions:
            return None

        size_usd = self._kelly_size(signal.score)
        entry_price = self._apply_slippage(signal.entry_price, signal.side, entry=True)
        commission = self._apply_commission(size_usd)

        if size_usd + commission > self.balance:
            return None

        self.balance -= commission
        quantity = size_usd / entry_price

        trail_pct = (
            self.config.scanner_config.base_trail_pct_scalp
            if signal.tier == "scalp"
            else self.config.scanner_config.base_trail_pct_swing
        )
        max_hold = (
            self.config.scanner_config.max_hold_ms_scalp
            if signal.tier == "scalp"
            else self.config.scanner_config.max_hold_ms_swing
        )

        if signal.side == "long":
            stop_price = entry_price * (1 - trail_pct)
        else:
            stop_price = entry_price * (1 + trail_pct)

        pos = Position(
            id=str(uuid.uuid4()), symbol=signal.symbol, product_id=signal.product_id,
            strategy=signal.strategy, side=signal.side, tier=signal.tier,
            entry_price=entry_price, quantity=quantity, size_usd=size_usd,
            opened_at=now_ms, high_watermark=entry_price, low_watermark=entry_price,
            current_price=entry_price, trail_pct=trail_pct,
            stop_price=stop_price, max_hold_ms=max_hold,
            qual_score=signal.score, signal_id=signal.id,
            status="open", paper_trading=True,
        )
        self.open_positions.append(pos)
        return pos

    def _update_positions(self, candle: dict, now_ms: float) -> None:
        """Update all open positions with the current candle and close if triggered."""
        high = candle["high"]
        low = candle["low"]
        close = candle["close"]

        still_open: list[Position] = []

        for pos in self.open_positions:
            if pos.symbol.upper().replace("USDT", "") not in candle.get("_symbol", pos.symbol):
                # This candle is for a different symbol — only update matching ones
                pass

            # Update watermarks
            pos.high_watermark = max(pos.high_watermark, high)
            pos.low_watermark = min(pos.low_watermark, low)
            pos.current_price = close

            # Update trailing stop
            if pos.side == "long":
                new_stop = pos.high_watermark * (1 - pos.trail_pct)
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
            else:
                new_stop = pos.low_watermark * (1 + pos.trail_pct)
                if new_stop < pos.stop_price:
                    pos.stop_price = new_stop

            # Check exit conditions
            exit_reason = None

            # Trailing stop hit
            if pos.side == "long" and low <= pos.stop_price:
                exit_reason = "trailing_stop"
                exit_price = pos.stop_price
            elif pos.side == "short" and high >= pos.stop_price:
                exit_reason = "trailing_stop"
                exit_price = pos.stop_price

            # Take profit (target price reached)
            if exit_reason is None and hasattr(pos, "_target_price") and pos._target_price:
                if pos.side == "long" and high >= pos._target_price:
                    exit_reason = "take_profit"
                    exit_price = pos._target_price
                elif pos.side == "short" and low <= pos._target_price:
                    exit_reason = "take_profit"
                    exit_price = pos._target_price

            # Time limit
            if exit_reason is None and (now_ms - pos.opened_at) >= pos.max_hold_ms:
                exit_reason = "time_limit"
                exit_price = close

            if exit_reason:
                exit_price = self._apply_slippage(exit_price, pos.side, entry=False)
                commission = self._apply_commission(pos.size_usd)

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
                pos.status = "closed"

                self.balance += pos.size_usd + pnl_usd
                self.closed_positions.append(pos)
            else:
                still_open.append(pos)

        self.open_positions = still_open

    def _compute_portfolio_metrics(self) -> PortfolioMetrics:
        """Compute metrics from closed positions (mirrors the live metrics engine)."""
        trades = self.closed_positions
        if not trades:
            return PortfolioMetrics(
                total_trades=0, win_rate=0, profit_factor=0, total_pnl_usd=0,
                sharpe_ratio=None, sortino_ratio=None, calmar_ratio=None,
                max_drawdown_pct=0, avg_hold_hours=0,
            )

        pnl_pcts = [t.pnl_pct or 0 for t in trades]
        pnl_usds = [t.pnl_usd or 0 for t in trades]
        wins_usd = [p for p in pnl_usds if p > 0]
        losses_usd = [p for p in pnl_usds if p <= 0]

        gross_wins = sum(wins_usd)
        gross_losses = abs(sum(losses_usd))

        win_rate = len(wins_usd) / len(trades)
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0)

        hold_hours = [
            (t.closed_at - t.opened_at) / 3_600_000 if t.closed_at else 0
            for t in trades
        ]
        avg_hold_hours = _mean(hold_hours)

        pnl_mean = _mean(pnl_pcts)
        pnl_std = _std_dev(pnl_pcts, pnl_mean)
        downside = [p for p in pnl_pcts if p < 0]
        downside_std = _std_dev(downside, 0)

        sharpe = (pnl_mean / pnl_std) * math.sqrt(252) if pnl_std > 0 and len(pnl_pcts) >= 30 else None
        sortino = (pnl_mean / downside_std) * math.sqrt(252) if downside_std > 0 and len(pnl_pcts) >= 30 else None
        sharpe = safe_ratio(sharpe) if sharpe is not None else None
        sortino = safe_ratio(sortino) if sortino is not None else None

        max_dd = _max_drawdown(pnl_usds)
        total_pnl = sum(pnl_usds)
        calmar = total_pnl / max_dd if max_dd > 0 and total_pnl > 0 else None
        calmar = safe_ratio(calmar) if calmar is not None else None

        # Per-strategy breakdown
        strategy_map: dict[str, list[Position]] = {}
        for t in trades:
            strategy_map.setdefault(t.strategy, []).append(t)

        by_strategy: list[StrategyMetrics] = []
        for strategy, st_trades in strategy_map.items():
            st_pnls = [t.pnl_pct or 0 for t in st_trades]
            st_wins = [p for p in st_pnls if p > 0]
            st_losses = [p for p in st_pnls if p <= 0]
            st_win_rate = len(st_wins) / len(st_trades)
            avg_win = _mean(st_wins) if st_wins else 0
            avg_loss = abs(_mean(st_losses)) if st_losses else 0

            by_strategy.append(StrategyMetrics(
                strategy=strategy,
                total_trades=len(st_trades),
                win_rate=st_win_rate,
                avg_win_pct=avg_win,
                avg_loss_pct=avg_loss,
                profit_factor=(st_win_rate * avg_win) / ((1 - st_win_rate) * avg_loss) if avg_loss > 0 and st_win_rate < 1 else 0,
                avg_hold_hours=_mean([(t.closed_at - t.opened_at) / 3_600_000 if t.closed_at else 0 for t in st_trades]),
                total_pnl_usd=sum(t.pnl_usd or 0 for t in st_trades),
                kelly_fraction=_kelly_fraction(st_win_rate, avg_win, avg_loss),
                max_consec_losses=_max_consecutive_losses(st_pnls),
            ))

        by_strategy.sort(key=lambda s: s.total_pnl_usd, reverse=True)

        return PortfolioMetrics(
            total_trades=len(trades), win_rate=win_rate, profit_factor=profit_factor,
            total_pnl_usd=total_pnl, sharpe_ratio=sharpe, sortino_ratio=sortino,
            calmar_ratio=calmar, max_drawdown_pct=max_dd,
            avg_hold_hours=avg_hold_hours, by_strategy=by_strategy,
        )

    def run(self) -> BacktestResult:
        """Run the backtest. Iterates through historical candles and simulates trading."""
        start_ms = _date_to_ms(self.config.start_date)
        end_ms = _date_to_ms(self.config.end_date)
        interval = self.config.interval
        config = self.config.scanner_config

        # Load data for all symbols
        symbol_candles: dict[str, list[dict]] = {}
        for symbol in self.config.symbols:
            candles = load_klines(symbol, interval, start_ms, end_ms)
            if candles:
                symbol_candles[symbol] = candles

        if not symbol_candles:
            return BacktestResult(
                metrics=PortfolioMetrics(
                    total_trades=0, win_rate=0, profit_factor=0, total_pnl_usd=0,
                    sharpe_ratio=None, sortino_ratio=None, calmar_ratio=None,
                    max_drawdown_pct=0, avg_hold_hours=0,
                ),
                total_trades=0,
                final_balance=self.balance,
                max_drawdown_pct=0,
                positions=[],
                equity_curve=[(start_ms, self.balance)],
            )

        # Build a unified timeline of all candle timestamps
        all_timestamps: set[int] = set()
        for candles in symbol_candles.values():
            for c in candles:
                all_timestamps.add(c["open_time"])
        timeline = sorted(all_timestamps)

        # Index candles by (symbol, timestamp) for fast lookup
        candle_index: dict[str, dict[int, dict]] = {}
        for symbol, candles in symbol_candles.items():
            candle_index[symbol] = {c["open_time"]: c for c in candles}

        # Track rolling window of candles per symbol for indicator computation
        candle_windows: dict[str, list[dict]] = {s: [] for s in symbol_candles}
        max_window = 50  # keep last 50 candles for indicators

        peak_equity = self.balance
        max_dd = 0.0

        for ts in timeline:
            now_ms = float(ts)

            # Update candle windows
            for symbol in symbol_candles:
                candle = candle_index[symbol].get(ts)
                if candle:
                    candle_windows[symbol].append(candle)
                    if len(candle_windows[symbol]) > max_window:
                        candle_windows[symbol] = candle_windows[symbol][-max_window:]

            # Update existing positions with each symbol's candle
            for symbol in symbol_candles:
                candle = candle_index[symbol].get(ts)
                if candle:
                    # Only update positions for this symbol
                    positions_for_symbol = [p for p in self.open_positions if p.symbol == symbol]
                    others = [p for p in self.open_positions if p.symbol != symbol]
                    self.open_positions = positions_for_symbol
                    self._update_positions(candle, now_ms)
                    self.open_positions = self.open_positions + others

            # Scan for new signals
            for symbol in symbol_candles:
                window = candle_windows[symbol]
                if len(window) < 5:
                    continue

                # Skip if we already have an open position for this symbol
                if any(p.symbol == symbol for p in self.open_positions):
                    continue

                # Try momentum
                signal = self._scan_momentum(symbol, window, config, now_ms)
                if signal:
                    self._open_position(signal, now_ms)
                    continue

                # Try mean reversion
                signal = self._scan_mean_reversion(symbol, window, config, now_ms)
                if signal:
                    self._open_position(signal, now_ms)

            # Record equity
            open_pnl = 0.0
            for pos in self.open_positions:
                if pos.side == "long":
                    open_pnl += pos.size_usd * ((pos.current_price - pos.entry_price) / pos.entry_price)
                else:
                    open_pnl += pos.size_usd * ((pos.entry_price - pos.current_price) / pos.entry_price)

            equity = self.balance + sum(p.size_usd for p in self.open_positions) + open_pnl
            self.equity_curve.append((ts, equity))

            if equity > peak_equity:
                peak_equity = equity
            if peak_equity > 0:
                dd = (peak_equity - equity) / peak_equity
                if dd > max_dd:
                    max_dd = dd

        # Force-close any remaining open positions at last available price
        for pos in list(self.open_positions):
            last_candle_list = candle_windows.get(pos.symbol, [])
            if last_candle_list:
                last_price = last_candle_list[-1]["close"]
            else:
                last_price = pos.current_price

            exit_price = self._apply_slippage(last_price, pos.side, entry=False)
            commission = self._apply_commission(pos.size_usd)

            if pos.side == "long":
                pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

            pnl_usd = pos.size_usd * pnl_pct - commission
            pos.exit_price = exit_price
            pos.closed_at = float(timeline[-1]) if timeline else pos.opened_at
            pos.pnl_pct = pnl_pct
            pos.pnl_usd = pnl_usd
            pos.exit_reason = "time_limit"
            pos.status = "closed"
            self.balance += pos.size_usd + pnl_usd
            self.closed_positions.append(pos)

        self.open_positions = []

        metrics = self._compute_portfolio_metrics()

        return BacktestResult(
            metrics=metrics,
            total_trades=len(self.closed_positions),
            final_balance=self.balance,
            max_drawdown_pct=max_dd,
            positions=self.closed_positions,
            equity_curve=self.equity_curve,
        )
