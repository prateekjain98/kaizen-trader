"""Self-Healing AI Crypto Trader — Main Entry Point."""

import http.server
import inspect
import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import asdict
from typing import Optional

from src.config import env, default_scanner_config, validate_config
from src.evaluation.strategy_selector import StrategySelector, SelectionConfig
from src.execution.paper import paper_buy, paper_sell, get_paper_balance
from src.execution.coinbase import place_buy_order, place_sell_order
from src.feeds.coinbase_ws import CoinbaseWebSocket
from src.qualification.scorer import qualify
from src.risk.portfolio import (
    init_protections, can_open_position, register_open, register_close,
    update_position_price, get_open_positions,
)
from src.risk.position_sizer import kelly_size, apply_correlation_discount
from src.risk.protections import DEFAULT_PROTECTIONS
from src.self_healing.log_analyzer import run_analysis
from src.self_healing.healer import on_position_closed
from src.self_healing.delta_evaluator import get_evaluator
from src.signals.fear_greed import fetch_fear_greed, build_market_context, fear_greed_to_market_phase
from src.signals.news import fetch_news_sentiment, NewsSentiment
from src.signals.social import fetch_social_sentiment, SocialSentiment
from src.signals.funding import fetch_funding_data
from src.signals.whale import poll_whale_alerts
from src.signals.protocol import fetch_protocol_revenue
from src.signals.token_unlocks import fetch_token_unlocks, is_unlock_risk, TokenUnlock
from src.signals.options import fetch_options_sentiment, OptionsSentiment
from src.signals.stablecoin import fetch_stablecoin_flows, StablecoinFlows
from src.signals.derivatives import fetch_derivatives_data, DerivativesData
from src.storage.database import (
    log, insert_position, insert_trade, update_position_close,
    get_closed_trades, batch_writes, close as close_db,
    init_dual_write,
)
from src.strategies.registry import get_registry
from src.strategies.momentum import push_price_sample, scan_momentum
from src.strategies.mean_reversion import push_ohlcv_sample, scan_mean_reversion
from src.strategies.orderbook_imbalance import (
    update_order_book, scan_orderbook_imbalance, OrderBookLevel,
)
from src.strategies.funding_extreme import scan_funding_extreme, update_funding_data, FundingRateData
from src.strategies.whale_tracker import scan_whale_accumulation
from src.strategies.liquidation_cascade import scan_liquidation_cascade
from src.strategies.fear_greed_contrarian import scan_fear_greed_contrarian
from src.strategies.correlation_break import scan_correlation_break
from src.strategies.narrative_momentum import scan_narrative_momentum
from src.strategies.protocol_revenue import scan_protocol_revenue, ProtocolMetrics
from src.indicators.core import (
    push_tick as push_indicator_tick, compute_atr_stop, compute_atr_trailing_stop,
    get_atr, _aggregate_to_htf,
)
from src.indicators.cvd import push_trade as push_cvd_trade, get_cvd_snapshot
from src.indicators.regime import classify_regime, get_regime_score
from src.types import ScannerConfig, MarketContext, Position, TradeSignal

# ─── Default watchlist (Coinbase product IDs) ────────────────────────────────
DEFAULT_WATCHLIST = [
    "BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD",
    "UNI-USD", "AAVE-USD", "ARB-USD", "OP-USD", "DOGE-USD",
    "MATIC-USD", "SUI-USD", "APT-USD", "SEI-USD", "TIA-USD",
    "ONDO-USD", "LDO-USD", "RENDER-USD", "FET-USD", "INJ-USD",
]

# Mutable config — self-healer patches this live
config = ScannerConfig(**asdict(default_scanner_config))

# Darwinian strategy selector
strategy_selector = StrategySelector(SelectionConfig())

_STRATEGY_EVAL_INTERVAL_S = 3600  # evaluate strategy health every hour
_EXIT_CHECK_INTERVAL_S = 5        # check exits every 5 seconds (swing/normal)
_SCALP_EXIT_CHECK_INTERVAL_S = 1  # check scalp exits every 1 second
_MARKET_CONTEXT_INTERVAL_S = 120  # refresh market context every 2 minutes
_SIGNAL_REFRESH_INTERVAL_S = 150  # refresh external signals every 2.5 minutes
_BOOK_PURGE_INTERVAL_S = 120      # purge stale order books every 2 minutes

# Graceful shutdown event — background threads check this to exit promptly
_shutdown_event = threading.Event()

# ─── Shared state (thread-safe) ──────────────────────────────────────────────
_market_ctx_lock = threading.Lock()
_market_ctx: MarketContext = MarketContext(
    phase="neutral", btc_dominance=50.0, fear_greed_index=50,
    total_market_cap_change_d1=0, timestamp=0,
)

_price_lock = threading.Lock()
_latest_prices: dict[str, float] = {}  # symbol -> latest price
_prev_prices: dict[str, float] = {}    # symbol -> previous tick price (for CVD side inference)

_news_lock = threading.Lock()
_news_cache: dict[str, NewsSentiment] = {}  # symbol -> NewsSentiment

_social_lock = threading.Lock()
_social_cache: dict[str, SocialSentiment] = {}  # symbol -> SocialSentiment

_options_lock = threading.Lock()
_options_cache: dict[str, OptionsSentiment] = {}  # symbol -> OptionsSentiment

_derivatives_lock = threading.Lock()
_derivatives_cache: dict[str, DerivativesData] = {}  # symbol -> DerivativesData

_stablecoin_lock = threading.Lock()
_stablecoin_cache: Optional[StablecoinFlows] = None

_unlock_risks: set[str] = set()  # symbols with upcoming large unlocks
_unlock_lock = threading.Lock()

# Tick-driven scan throttle: avoid scanning on every single tick
_last_scan_time: dict[str, float] = {}
_MIN_SCAN_INTERVAL_S = 2.0  # at most one full scan per symbol every 2s
_last_htf_aggregate: dict[str, float] = {}  # symbol -> last HTF aggregation time
_HTF_AGGREGATE_INTERVAL_S = 60.0  # aggregate to higher timeframes every 60 seconds


# ─── Market context ──────────────────────────────────────────────────────────

def _build_market_context() -> MarketContext:
    """Fetch fear/greed index and build a MarketContext."""
    fgi = fetch_fear_greed()
    if fgi:
        # Use BTC dominance from CoinGecko if available; default to 50
        ctx = build_market_context(fgi, btc_dominance=50.0)
        return ctx
    # Fallback: return neutral context
    return MarketContext(
        phase="neutral", btc_dominance=50.0, fear_greed_index=50,
        total_market_cap_change_d1=0, timestamp=time.time() * 1000,
    )


def _refresh_market_context() -> None:
    """Update the shared market context. Called periodically."""
    global _market_ctx
    try:
        ctx = _build_market_context()
        # Enrich with regime detection
        regime = classify_regime("BTC")
        # Override phase with regime-detected phase if available
        if regime.phase != "unknown":
            ctx.phase = regime.phase
        with _market_ctx_lock:
            _market_ctx = ctx
        log("info",
            f"Market context refreshed: phase={ctx.phase} FGI={ctx.fear_greed_index} "
            f"regime={regime.trend}/{regime.volatility} score={regime.regime_score:.0f}")
    except Exception as err:
        log("error", f"Failed to refresh market context: {err}")


def _get_market_context() -> MarketContext:
    with _market_ctx_lock:
        return _market_ctx


# ─── Signal refresh ──────────────────────────────────────────────────────────

def _get_watchlist_symbols() -> list[str]:
    """Extract bare symbols from product IDs."""
    return [pid.replace("-USD", "") for pid in DEFAULT_WATCHLIST]


def _refresh_signals() -> None:
    """Fetch news, social, funding, whale, and protocol revenue data."""
    symbols = _get_watchlist_symbols()

    # News sentiment
    try:
        news_list = fetch_news_sentiment(symbols)
        with _news_lock:
            _news_cache.clear()
            for ns in news_list:
                _news_cache[ns.symbol] = ns
    except Exception as err:
        log("error", f"News sentiment refresh failed: {err}")

    # Social sentiment
    try:
        social_list = fetch_social_sentiment(symbols)
        with _social_lock:
            _social_cache.clear()
            for ss in social_list:
                _social_cache[ss.symbol] = ss
    except Exception as err:
        log("error", f"Social sentiment refresh failed: {err}")

    # Funding rates -> update funding_extreme strategy cache
    try:
        funding_list = fetch_funding_data(symbols)
        for fd in funding_list:
            update_funding_data(FundingRateData(
                symbol=fd.symbol,
                funding_rate=fd.funding_rate,
                funding_interval_hours=8,
                open_interest=fd.open_interest_usd,
                open_interest_change_pct=fd.open_interest_change_24h * 100,
                predicted_rate=None,
            ))
    except Exception as err:
        log("error", f"Funding data refresh failed: {err}")

    # Whale alerts
    try:
        poll_whale_alerts(symbols)
    except Exception as err:
        log("error", f"Whale alert poll failed: {err}")

    # Protocol revenue (only triggers scan for protocol_revenue strategy)
    try:
        _scan_protocol_revenue_signals()
    except Exception as err:
        log("error", f"Protocol revenue scan failed: {err}")

    # Token unlock risk detection
    try:
        risk_symbols = set()
        for sym in symbols:
            if is_unlock_risk(sym):
                risk_symbols.add(sym)
        with _unlock_lock:
            _unlock_risks.clear()
            _unlock_risks.update(risk_symbols)
        if risk_symbols:
            log("info", f"Token unlock risk active for: {', '.join(sorted(risk_symbols))}")
    except Exception as err:
        log("error", f"Token unlock check failed: {err}")

    # Options sentiment (BTC + ETH only)
    for sym in ("BTC", "ETH"):
        try:
            opt = fetch_options_sentiment(sym)
            if opt:
                with _options_lock:
                    _options_cache[sym] = opt
        except Exception as err:
            log("error", f"Options sentiment fetch failed for {sym}: {err}")

    # Stablecoin flows (global, not per-symbol)
    try:
        global _stablecoin_cache
        flows = fetch_stablecoin_flows()
        if flows:
            with _stablecoin_lock:
                _stablecoin_cache = flows
    except Exception as err:
        log("error", f"Stablecoin flows fetch failed: {err}")

    # Derivatives data (futures basis, OI, funding from Binance)
    for sym in symbols[:10]:  # top 10 symbols only to respect rate limits
        try:
            deriv = fetch_derivatives_data(sym)
            if deriv:
                with _derivatives_lock:
                    _derivatives_cache[sym] = deriv
        except Exception as err:
            log("error", f"Derivatives fetch failed for {sym}: {err}")


def _scan_protocol_revenue_signals() -> None:
    """Fetch protocol revenue data and scan for trade signals."""
    if not strategy_selector.is_strategy_enabled("protocol_revenue"):
        return
    rev_data = fetch_protocol_revenue()
    ctx = _get_market_context()
    for pr in rev_data:
        with _price_lock:
            current_price = _latest_prices.get(pr.symbol)
        if not current_price:
            continue
        product_id = f"{pr.symbol}-USD"
        metric = ProtocolMetrics(
            symbol=pr.symbol, product_id=product_id,
            protocol=pr.protocol, revenue_24h=pr.revenue_24h,
            revenue_7d_avg=pr.revenue_7d_avg, tvl=0,
            tvl_change_7d=0, token_price_change_24h=0,
        )
        sig = scan_protocol_revenue(metric, current_price)
        if sig:
            _process_signal(sig, ctx)


# ─── Signal processing (qualify -> risk -> size -> execute) ───────────────────

def _process_signal(signal: TradeSignal, ctx: MarketContext) -> None:
    """Qualify a signal and open a position if it passes all checks."""
    # Check if strategy is enabled
    if not strategy_selector.is_strategy_enabled(signal.strategy):
        return

    # Get signal data for qualification
    with _news_lock:
        news = _news_cache.get(signal.symbol)
    with _social_lock:
        social = _social_cache.get(signal.symbol)
    with _options_lock:
        options = _options_cache.get(signal.symbol)
    with _derivatives_lock:
        derivatives = _derivatives_cache.get(signal.symbol)
    with _stablecoin_lock:
        stablecoin = _stablecoin_cache
    with _unlock_lock:
        has_unlock_risk = signal.symbol in _unlock_risks

    cvd = get_cvd_snapshot(signal.symbol)
    regime = classify_regime(signal.symbol)

    # Qualify
    qual = qualify(
        signal, ctx, config,
        news=news, social=social, cvd=cvd, regime=regime,
        options=options, derivatives=derivatives,
        stablecoin=stablecoin, has_unlock_risk=has_unlock_risk,
    )
    if not qual.passed:
        return

    log("signal",
        f"{signal.strategy} signal: {signal.symbol} {signal.side} (qual={qual.score:.0f}) — {signal.reasoning}",
        symbol=signal.symbol, strategy=signal.strategy,
        data={"qual_score": qual.score, "breakdown": qual.breakdown})

    # Risk check
    if not can_open_position():
        log("info", f"Risk manager blocked position for {signal.symbol}", symbol=signal.symbol)
        return

    # Position sizing
    portfolio_usd = get_paper_balance() if env.paper_trading else env.max_position_usd * env.max_open_positions
    size_usd = kelly_size(signal.strategy, portfolio_usd, qual.score)
    if size_usd <= 0:
        log("info", f"Kelly sizing returned 0 for {signal.strategy}", symbol=signal.symbol)
        return

    # Correlation-aware discount: reduce size when stacking correlated assets
    open_pos = get_open_positions()
    size_usd = apply_correlation_discount(size_usd, signal.symbol, signal.side, open_pos)

    # Determine trail and hold time based on tier
    if signal.tier == "scalp":
        trail_pct = config.base_trail_pct_scalp
        max_hold_ms = config.max_hold_ms_scalp
    else:
        trail_pct = config.base_trail_pct_swing
        max_hold_ms = config.max_hold_ms_swing

    # Execute
    now = time.time() * 1000
    position_id = str(uuid.uuid4())

    try:
        if env.paper_trading:
            trade = paper_buy(
                signal.symbol, signal.product_id, size_usd,
                position_id, signal.entry_price,
            )
        else:
            trade = place_buy_order(signal.product_id, size_usd, position_id)
    except Exception as err:
        log("error", f"Execution failed for {signal.symbol}: {err}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    if trade.status == "failed":
        log("error", f"Trade failed for {signal.symbol}: {trade.error}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Determine actual entry price and quantity from the fill
    entry_price = trade.price if trade.price > 0 else signal.entry_price
    quantity = trade.quantity

    # Compute ATR-based stop price (falls back to fixed % if ATR unavailable)
    stop_price, trail_pct = compute_atr_stop(
        signal.symbol, entry_price, signal.side, signal.strategy,
        fallback_trail_pct=trail_pct,
    )

    # Create position
    position = Position(
        id=position_id,
        symbol=signal.symbol,
        product_id=signal.product_id,
        strategy=signal.strategy,
        side=signal.side,
        tier=signal.tier,
        entry_price=entry_price,
        quantity=quantity,
        size_usd=trade.size_usd,
        opened_at=now,
        high_watermark=entry_price,
        low_watermark=entry_price,
        current_price=entry_price,
        trail_pct=trail_pct,
        stop_price=stop_price,
        max_hold_ms=max_hold_ms,
        qual_score=qual.score,
        signal_id=signal.id,
        status="open",
        paper_trading=env.paper_trading,
        original_quantity=quantity,
    )

    # Persist and register
    with batch_writes():
        insert_position(position)
        insert_trade(trade)
    register_open(position)

    log("trade",
        f"OPENED {signal.side.upper()} {signal.symbol} ${trade.size_usd:.0f} "
        f"@ {entry_price:.4f} (strategy={signal.strategy} qual={qual.score:.0f} "
        f"trail={trail_pct*100:.1f}%)",
        symbol=signal.symbol, strategy=signal.strategy,
        data={
            "position_id": position_id, "size_usd": trade.size_usd,
            "entry_price": entry_price, "stop_price": stop_price,
            "qual_score": qual.score, "trail_pct": trail_pct,
        })


# ─── Tick-driven strategy scanning ───────────────────────────────────────────

def _on_tick(symbol: str, price: float, volume: float) -> None:
    """Called on each WebSocket tick. Feed data to strategies and scan."""
    # Update latest price
    with _price_lock:
        _latest_prices[symbol] = price

    # Update open position prices
    for pos in get_open_positions():
        if pos.symbol == symbol:
            update_position_price(pos.id, price)

    # Feed price samples to tick-driven strategies and indicator engine
    push_price_sample(symbol, price, volume)
    push_ohlcv_sample(symbol, price, volume)
    push_indicator_tick(symbol, price, volume)

    # Infer trade side from price movement for CVD
    prev_price = _prev_prices.get(symbol, price)
    _prev_prices[symbol] = price
    inferred_side = "buy" if price >= prev_price else "sell"
    push_cvd_trade(symbol, price, volume, inferred_side)

    # Periodically aggregate minute candles to higher timeframes
    now_s = time.time()
    last_agg = _last_htf_aggregate.get(symbol, 0)
    if now_s - last_agg >= _HTF_AGGREGATE_INTERVAL_S:
        _last_htf_aggregate[symbol] = now_s
        _aggregate_to_htf(symbol)

    # Throttle scanning
    now = time.time()
    last_scan = _last_scan_time.get(symbol, 0)
    if now - last_scan < _MIN_SCAN_INTERVAL_S:
        return
    _last_scan_time[symbol] = now

    # Get shared context
    ctx = _get_market_context()
    product_id = f"{symbol}-USD"

    # --- Tick-driven strategies ---
    # Each strategy has a different signature; call them explicitly.

    # 1. Momentum (swing + scalp)
    _try_scan(lambda: scan_momentum(symbol, product_id, price, config, ctx), ctx)

    # 2. Mean reversion
    _try_scan(lambda: scan_mean_reversion(symbol, product_id, price, config, ctx), ctx)

    # 3. Funding extreme
    _try_scan(lambda: scan_funding_extreme(symbol, product_id, price, config, ctx), ctx)

    # 4. Whale accumulation (needs whale data from poll_whale_alerts)
    _try_scan(lambda: scan_whale_accumulation(symbol, product_id, price), ctx)

    # 5. Liquidation cascade
    _try_scan(lambda: scan_liquidation_cascade(symbol, product_id, price, config, ctx), ctx)

    # 6. Fear & Greed contrarian
    _try_scan(lambda: scan_fear_greed_contrarian(symbol, product_id, price, ctx), ctx)

    # 7. Correlation break (requires BTC % change data)
    _try_scan_correlation(symbol, product_id, price, ctx)

    # 8. Orderbook imbalance (scanned on book updates, but also scan here)
    _try_scan(lambda: scan_orderbook_imbalance(symbol, product_id, price, config), ctx)


def _try_scan(scan_fn, ctx: MarketContext) -> None:
    """Execute a scan function, process any signal returned."""
    try:
        sig = scan_fn()
        if sig:
            _process_signal(sig, ctx)
    except Exception as err:
        log("error", f"Strategy scan error: {err}")


def _try_scan_correlation(symbol: str, product_id: str, price: float, ctx: MarketContext) -> None:
    """Scan correlation break strategy using BTC as reference."""
    if symbol == "BTC":
        return  # BTC is the reference, not a tradeable pair for this strategy
    with _price_lock:
        btc_price = _latest_prices.get("BTC")
    if not btc_price:
        return
    # Use a simple approximation: 0% change for both when we lack history
    # The strategy's internal correlation history will handle accumulation
    try:
        sig = scan_correlation_break(
            symbol, product_id, price,
            btc_1h_pct=0, alt_1h_pct=0,
            config=config, ctx=ctx,
        )
        if sig:
            _process_signal(sig, ctx)
    except Exception as err:
        log("error", f"Correlation break scan error for {symbol}: {err}")


def _scan_narrative_momentum(ctx: MarketContext) -> None:
    """Run narrative momentum strategy (not tick-driven, runs periodically)."""
    if not strategy_selector.is_strategy_enabled("narrative_momentum"):
        return
    with _price_lock:
        current_prices = dict(_latest_prices)
    product_id_map = {sym: f"{sym}-USD" for sym in current_prices}
    try:
        sig = scan_narrative_momentum(product_id_map, config, current_prices)
        if sig:
            _process_signal(sig, ctx)
    except Exception as err:
        log("error", f"Narrative momentum scan error: {err}")


# ─── Order book updates ──────────────────────────────────────────────────────

def _on_book(symbol: str, bids: list, asks: list) -> None:
    """Called on each WebSocket L2 update."""
    try:
        bid_levels = [OrderBookLevel(price=b["price"], size=b["size"]) for b in bids]
        ask_levels = [OrderBookLevel(price=a["price"], size=a["size"]) for a in asks]
        update_order_book(symbol, bid_levels, ask_levels)
    except Exception as err:
        log("error", f"Order book update error for {symbol}: {err}")


# ─── Exit checking ────────────────────────────────────────────────────────────


def _compute_r_multiple(pos: Position, current_price: float) -> float:
    """Compute R-multiple: how many R (risk units) the trade has moved in our favor.

    R = distance from entry to initial stop. R-multiple = profit / R.
    """
    if pos.entry_price <= 0:
        return 0.0
    initial_risk = abs(pos.entry_price - pos.stop_price) if pos.stop_price > 0 else pos.entry_price * pos.trail_pct
    if initial_risk <= 0:
        return 0.0
    if pos.side == "long":
        profit = current_price - pos.entry_price
    else:
        profit = pos.entry_price - current_price
    return profit / initial_risk


def _execute_partial_exit(pos: Position, current_price: float, now: float, fraction: float = 0.5) -> None:
    """Sell a fraction of the position as a partial take-profit."""
    partial_qty = pos.quantity * fraction
    if partial_qty <= 0:
        return

    try:
        if env.paper_trading:
            trade = paper_sell(pos.symbol, pos.product_id, partial_qty, pos.id, current_price)
        else:
            trade = place_sell_order(pos.product_id, partial_qty, pos.id)
    except Exception as err:
        log("warn", f"Partial exit failed for {pos.symbol}: {err}",
            symbol=pos.symbol, strategy=pos.strategy)
        return

    if trade.status == "failed":
        return

    # Track the partial exit
    if pos.original_quantity is None:
        pos.original_quantity = pos.quantity
    pos.quantity -= partial_qty
    pos.partial_exit_pct += fraction
    pos.size_usd = pos.quantity * current_price

    insert_trade(trade)

    r_mult = _compute_r_multiple(pos, current_price)
    log("trade",
        f"PARTIAL EXIT {pos.side.upper()} {pos.symbol} — sold {fraction*100:.0f}% "
        f"@ {current_price:.4f} (R={r_mult:.1f}), trailing remainder",
        symbol=pos.symbol, strategy=pos.strategy)


def _check_single_exit(pos: Position, now: float, ctx: MarketContext) -> None:
    """Check a single position for exit conditions and execute if needed."""
    with _price_lock:
        current_price = _latest_prices.get(pos.symbol)
    if not current_price or current_price <= 0:
        return

    # Update watermarks
    if current_price > pos.high_watermark:
        pos.high_watermark = current_price
    if current_price < pos.low_watermark:
        pos.low_watermark = current_price
    pos.current_price = current_price

    # Update MAE/MFE (Maximum Adverse/Favorable Excursion)
    if pos.entry_price > 0:
        excursion_pct = (current_price - pos.entry_price) / pos.entry_price
        if pos.side == "short":
            excursion_pct = -excursion_pct
        if excursion_pct > pos.mfe_pct:
            pos.mfe_pct = excursion_pct
        if excursion_pct < pos.mae_pct:
            pos.mae_pct = excursion_pct

    # Update trailing stop using ATR (only tightens, never widens)
    if pos.side == "long":
        pos.stop_price = compute_atr_trailing_stop(
            pos.symbol, pos.high_watermark, pos.side, pos.strategy,
            pos.stop_price, pos.trail_pct,
        )
    else:
        pos.stop_price = compute_atr_trailing_stop(
            pos.symbol, pos.low_watermark, pos.side, pos.strategy,
            pos.stop_price, pos.trail_pct,
        )

    # Partial take-profit: sell 50% at 1.5R, trail the rest
    if pos.partial_exit_pct == 0.0 and pos.entry_price > 0:
        r_multiple = _compute_r_multiple(pos, current_price)
        if r_multiple >= 1.5:
            _execute_partial_exit(pos, current_price, now, fraction=0.5)

    # Determine exit reason
    exit_reason = None

    # 1. Trailing stop hit
    if pos.side == "long" and current_price <= pos.stop_price:
        exit_reason = "trailing_stop"
    elif pos.side == "short" and current_price >= pos.stop_price:
        exit_reason = "trailing_stop"

    # 2. Max hold time exceeded
    hold_ms = now - pos.opened_at
    if hold_ms >= pos.max_hold_ms:
        exit_reason = "time_limit"

    if not exit_reason:
        return

    # Execute sell
    try:
        if env.paper_trading:
            trade = paper_sell(
                pos.symbol, pos.product_id, pos.quantity,
                pos.id, current_price,
            )
        else:
            trade = place_sell_order(pos.product_id, pos.quantity, pos.id)
    except Exception as err:
        log("error", f"Exit execution failed for {pos.symbol}: {err}",
            symbol=pos.symbol, strategy=pos.strategy)
        return

    if trade.status == "failed":
        log("error", f"Exit trade failed for {pos.symbol}: {trade.error}",
            symbol=pos.symbol, strategy=pos.strategy)
        return

    exit_price = trade.price if trade.price > 0 else current_price

    # Compute PnL
    if pos.side == "long":
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0
    else:
        pnl_pct = (pos.entry_price - exit_price) / pos.entry_price if pos.entry_price > 0 else 0
    pnl_usd = pnl_pct * pos.size_usd

    # Update position fields for self-healing
    pos.exit_price = exit_price
    pos.closed_at = now
    pos.pnl_usd = pnl_usd
    pos.pnl_pct = pnl_pct
    pos.exit_reason = exit_reason
    pos.status = "closed"

    # Persist
    with batch_writes():
        update_position_close(pos.id, exit_price, pnl_usd, pnl_pct, exit_reason)
        insert_trade(trade)

    # Register close with risk manager
    register_close(pos, pnl_usd)

    # Self-healing: diagnose the trade
    on_position_closed(pos, config, ctx.phase)

    # Darwinian strategy evaluation
    strategy_selector.on_trade_closed(pos)

    pnl_sign = "+" if pnl_pct >= 0 else ""
    log("trade",
        f"CLOSED {pos.side.upper()} {pos.symbol} @ {exit_price:.4f} "
        f"PnL {pnl_sign}{pnl_pct*100:.2f}% (${pnl_sign}{pnl_usd:.2f}) "
        f"reason={exit_reason} hold={((now - pos.opened_at) / 3_600_000):.1f}h",
        symbol=pos.symbol, strategy=pos.strategy,
        data={
            "position_id": pos.id, "exit_price": exit_price,
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "exit_reason": exit_reason, "strategy": pos.strategy,
            "mae_pct": pos.mae_pct, "mfe_pct": pos.mfe_pct,
            "partial_exit_pct": pos.partial_exit_pct,
        })


# ─── Background loops ────────────────────────────────────────────────────────

def _analysis_loop() -> None:
    """Periodically run Claude log analysis."""
    while not _shutdown_event.is_set():
        # Use wait() instead of sleep() so we respond to shutdown promptly
        if _shutdown_event.wait(timeout=env.log_analysis_interval_mins * 60):
            break
        try:
            run_analysis(config, strategy_selector=strategy_selector)
        except Exception as err:
            log("error", f"Log analysis failed: {err}")


def _strategy_eval_loop() -> None:
    """Periodically evaluate strategy health and disable underperformers."""
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=_STRATEGY_EVAL_INTERVAL_S):
            break
        try:
            trades = get_closed_trades(200)
            results = strategy_selector.evaluate_strategies(trades)
            disabled = [h for h in results if not h.enabled]
            if disabled:
                names = ", ".join(h.strategy_id for h in disabled)
                reasons = "; ".join(f"{h.strategy_id}: {h.disable_reason}" for h in disabled)
                log("warn", f"Darwinian selection disabled {len(disabled)} strategies: {names}",
                    data={"disabled": reasons})
            else:
                log("info", f"Strategy evaluation complete — all {len(results)} strategies healthy")

            # Evaluate pending parameter deltas and auto-revert worsened ones
            evaluated = get_evaluator().evaluate_pending_deltas(config)
            if evaluated:
                reverted = [d for d in evaluated if d.verdict == "worsened"]
                improved = [d for d in evaluated if d.verdict == "improved"]
                neutral_count = len(evaluated) - len(improved) - len(reverted)
                log("info",
                    f"Delta evaluation: {len(improved)} improved, {len(reverted)} reverted, "
                    f"{neutral_count} neutral",
                    data={
                        "improved": [d.parameter for d in improved],
                        "reverted": [d.parameter for d in reverted],
                    })
        except Exception as err:
            log("error", f"Strategy evaluation failed: {err}")


_SCALP_STRATEGIES = {"momentum_scalp", "orderbook_imbalance", "liquidation_cascade", "listing_pump"}


def _exit_check_loop() -> None:
    """Periodically check open positions for exit conditions.

    Scalp strategies are checked every 1s; swing/normal every 5s.
    """
    tick = 0
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=_SCALP_EXIT_CHECK_INTERVAL_S):
            break
        try:
            now = time.time() * 1000
            positions = get_open_positions()
            ctx = _get_market_context()
            for pos in positions:
                try:
                    is_scalp = pos.strategy in _SCALP_STRATEGIES
                    # Scalp: every tick (1s). Others: every 5 ticks (5s).
                    if is_scalp or tick % _EXIT_CHECK_INTERVAL_S == 0:
                        _check_single_exit(pos, now, ctx)
                except Exception as err:
                    log("error", f"Exit check error for {pos.symbol} ({pos.id}): {err}",
                        symbol=pos.symbol, strategy=pos.strategy)
            tick += 1
        except Exception as err:
            log("error", f"Exit check loop error: {err}")


def _market_context_loop() -> None:
    """Periodically refresh market context."""
    # Do an initial fetch immediately
    _refresh_market_context()
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=_MARKET_CONTEXT_INTERVAL_S):
            break
        try:
            _refresh_market_context()
        except Exception as err:
            log("error", f"Market context loop error: {err}")


def _signal_refresh_loop() -> None:
    """Periodically refresh external signals (news, social, funding, whale, protocol)."""
    # Initial fetch
    _refresh_signals()
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=_SIGNAL_REFRESH_INTERVAL_S):
            break
        try:
            _refresh_signals()
            # Run non-tick-driven strategies after signal refresh
            ctx = _get_market_context()
            _scan_narrative_momentum(ctx)
        except Exception as err:
            log("error", f"Signal refresh loop error: {err}")


# ─── Utilities ────────────────────────────────────────────────────────────────

def _build_strategy_banner() -> str:
    """Build strategy list dynamically from the registry."""
    registry = get_registry()
    names = sorted(registry.keys())
    # Format in two columns
    lines = []
    for i in range(0, len(names), 2):
        left = names[i]
        right = f"* {names[i + 1]}" if i + 1 < len(names) else ""
        lines.append(f"    {left:<24}{right}")
    return "\n".join(lines)


def _start_thread(name: str, target) -> threading.Thread:
    """Start a daemon thread and return it."""
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


# ─── Health check HTTP server ────────────────────────────────────────────────

_start_time = time.time()
# Thread registry — populated in main() so the health endpoint can report status
_thread_registry: dict[str, threading.Thread] = {}


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for Railway health checks."""

    def do_GET(self):
        if self.path == "/health":
            open_positions = get_open_positions()
            registry = get_registry()

            threads_alive = {
                name: t.is_alive() for name, t in _thread_registry.items()
            }

            # Compute daily PnL from closed trades
            daily_pnl = 0.0
            try:
                today_start_ms = (time.time() - 86400) * 1000
                trades = get_closed_trades(500)
                daily_pnl = sum(
                    t.pnl_usd for t in trades
                    if hasattr(t, "pnl_usd") and t.pnl_usd is not None
                    and hasattr(t, "closed_at") and t.closed_at and t.closed_at >= today_start_ms
                )
            except Exception:
                pass

            # Last trade timestamp
            last_trade_at = 0
            try:
                recent = get_closed_trades(1)
                if recent and hasattr(recent[0], "closed_at") and recent[0].closed_at:
                    last_trade_at = int(recent[0].closed_at)
            except Exception:
                pass

            status = {
                "status": "healthy",
                "uptime_seconds": round(time.time() - _start_time),
                "paper_trading": env.paper_trading,
                "open_positions": len(open_positions),
                "strategies_count": len(registry),
                "last_trade_at": last_trade_at,
                "daily_pnl": round(daily_pnl, 2),
                "threads_alive": threads_alive,
            }

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default request logging


def _start_health_server() -> None:
    """Start a background HTTP server for health checks."""
    port = int(os.environ.get("PORT", "8080"))
    server = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    log("info", f"Health check server started on port {port}")


# ─── Main entry point ────────────────────────────────────────────────────────

def main() -> None:
    # Initialize declarative protection chain
    init_protections(DEFAULT_PROTECTIONS)

    log("info", "--- Self-Healing Crypto Trader starting ---",
        data={
            "paper_trading": env.paper_trading,
            "max_position_usd": env.max_position_usd,
            "log_analysis_interval_mins": env.log_analysis_interval_mins,
        })

    if env.paper_trading:
        log("info", "PAPER TRADING mode — no real orders will be placed")

    if not env.anthropic_api_key:
        log("warn", "ANTHROPIC_API_KEY not set — Claude log analysis disabled")

    # CONFIG_BOUNDS validation at startup
    violations = validate_config(config)
    if violations:
        log("warn", f"Config bounds violations at startup: {'; '.join(violations)}")
    else:
        log("info", "Config bounds validation passed")

    # ── Initialize dual-write backend (SQLite + Convex) if configured ─────
    convex_url = os.environ.get("CONVEX_URL")
    if convex_url:
        init_dual_write(convex_url)
        log("info", "Dual-write enabled: SQLite + Convex")

    # ── Start health check server (for Railway) ────────────────────────────
    _start_health_server()

    # ── Start background threads ──────────────────────────────────────────

    # Market context refresh (fetch fear/greed immediately)
    ctx_thread = _start_thread("market_ctx", _market_context_loop)
    log("info", f"Market context refresh scheduled every {_MARKET_CONTEXT_INTERVAL_S}s")

    # Signal refresh (news, social, funding, whale, protocol)
    signal_thread = _start_thread("signals", _signal_refresh_loop)
    log("info", f"Signal refresh scheduled every {_SIGNAL_REFRESH_INTERVAL_S}s")

    # Exit checking loop
    exit_thread = _start_thread("exits", _exit_check_loop)
    log("info", f"Exit check loop running every {_EXIT_CHECK_INTERVAL_S}s")

    # Claude log analysis loop
    analysis_thread = None
    if env.anthropic_api_key:
        analysis_thread = _start_thread("analysis", _analysis_loop)
        log("info", f"Claude log analysis scheduled every {env.log_analysis_interval_mins} minutes")

    # Darwinian strategy evaluation loop
    eval_thread = _start_thread("eval", _strategy_eval_loop)
    log("info", f"Darwinian strategy evaluation scheduled every {_STRATEGY_EVAL_INTERVAL_S // 60} minutes")

    # ── Register threads for health endpoint ────────────────────────────────
    _thread_registry["analysis"] = analysis_thread if analysis_thread else threading.Thread()
    _thread_registry["strategy_eval"] = eval_thread
    _thread_registry["exit_check"] = exit_thread
    _thread_registry["signal_refresh"] = signal_thread
    _thread_registry["market_context"] = ctx_thread

    # ── Connect WebSocket ─────────────────────────────────────────────────
    ws = CoinbaseWebSocket(
        product_ids=DEFAULT_WATCHLIST,
        on_tick=_on_tick,
        on_book=_on_book,
    )
    ws.connect()
    log("info", f"Coinbase WebSocket connecting to {len(DEFAULT_WATCHLIST)} products")

    # ── Strategy banner ───────────────────────────────────────────────────
    strategy_banner = _build_strategy_banner()
    registry = get_registry()
    log("info", f"""
----------------------------------------------
  Strategies ({len(registry)} auto-discovered):
{strategy_banner}

  Self-healing:
    immediate  — loss diagnosis + parameter patch after each trade
    periodic   — Claude log analysis every {env.log_analysis_interval_mins}m
    darwinian  — strategy health evaluation every {_STRATEGY_EVAL_INTERVAL_S // 60}m
    delta      — parameter change tracking + auto-revert if worsened

  Protections:
    {', '.join(r['rule_type'] for r in DEFAULT_PROTECTIONS)}

  Trading pipeline:
    WS tick -> strategy scan -> qualify -> risk check -> size -> execute -> track
    Exit check every {_EXIT_CHECK_INTERVAL_S}s | Market context every {_MARKET_CONTEXT_INTERVAL_S}s
    Signals refresh every {_SIGNAL_REFRESH_INTERVAL_S}s

  Blind spot detection: enabled (threshold=3 occurrences)
----------------------------------------------""")

    # Graceful shutdown handler
    if threading.current_thread() is threading.main_thread():
        def handle_sigint(sig, frame):
            log("info", "Received SIGINT — shutting down gracefully...")
            _shutdown_event.set()
        signal.signal(signal.SIGINT, handle_sigint)

    # Main loop with thread health monitoring
    threads = {
        "market_ctx": (ctx_thread, _market_context_loop),
        "signals": (signal_thread, _signal_refresh_loop),
        "exits": (exit_thread, _exit_check_loop),
        "eval": (eval_thread, _strategy_eval_loop),
    }
    if analysis_thread:
        threads["analysis"] = (analysis_thread, _analysis_loop)

    try:
        while not _shutdown_event.is_set():
            _shutdown_event.wait(timeout=5)

            # Check and restart threads if they died
            for name, (thread, target) in list(threads.items()):
                if not thread.is_alive():
                    log("error", f"{name} thread died — restarting")
                    new_thread = _start_thread(name, target)
                    threads[name] = (new_thread, target)

            # Check WebSocket health
            if not ws.is_connected():
                log("warn", "WebSocket disconnected — reconnect should be automatic")

    except KeyboardInterrupt:
        log("info", "Received KeyboardInterrupt — shutting down gracefully...")
        _shutdown_event.set()

    # Clean shutdown
    ws.disconnect()
    log("info", "Shutdown complete.")
    close_db()


if __name__ == "__main__":
    main()
