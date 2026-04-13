"""Self-Healing AI Crypto Trader — Main Entry Point."""

import hmac as _hmac_mod
import http.server
import json
import os
import signal
import sys
import threading
import time
import uuid
import dataclasses
from dataclasses import asdict
from typing import Optional

from src.config import env, default_scanner_config, validate_config
from src.evaluation.strategy_selector import StrategySelector, SelectionConfig
from src.execution.paper import get_paper_balance
from src.execution.router import execute_buy, execute_sell
from src.feeds.coinbase_ws import CoinbaseWebSocket
from src.qualification.scorer import qualify
from src.risk.portfolio import (
    init_protections, can_open_position, register_open, register_close,
    update_position_price, get_open_positions, compute_sharpe, compute_max_drawdown,
    compute_cvar,
)
from src.risk.position_sizer import kelly_size, apply_correlation_discount, check_sector_exposure, update_peak, log_kelly_rationale
from src.risk.protections import DEFAULT_PROTECTIONS
from src.self_healing.log_analyzer import run_analysis
from src.self_healing.healer import on_position_closed
from src.self_healing.delta_evaluator import get_evaluator
from src.signals.fear_greed import fetch_fear_greed, build_market_context
from src.signals.news import fetch_news_sentiment, NewsSentiment
from src.signals.social import fetch_social_sentiment, SocialSentiment
from src.signals.funding import fetch_funding_data
from src.signals.whale import poll_whale_alerts
from src.signals.protocol import fetch_protocol_revenue
from src.signals.token_unlocks import is_unlock_risk
from src.signals.options import fetch_options_sentiment, OptionsSentiment
from src.signals.stablecoin import fetch_stablecoin_flows, StablecoinFlows
from src.signals.derivatives import fetch_derivatives_data, fetch_leverage_profile, DerivativesData
import src.storage.database as _db_mod
from src.storage.database import (
    log, insert_position, insert_trade, update_position_close,
    update_position_price as db_update_position_price,
    get_closed_trades, batch_writes, close as close_db,
)
from src.strategies.registry import get_registry
from src.strategies.momentum import push_price_sample, scan_momentum
from src.strategies.mean_reversion import push_ohlcv_sample, scan_mean_reversion
from src.strategies.orderbook_imbalance import (
    update_order_book, scan_orderbook_imbalance, OrderBookLevel, get_bid_ask_spread,
)
from src.strategies.funding_extreme import scan_funding_extreme, update_funding_data, FundingRateData
from src.strategies.whale_tracker import scan_whale_accumulation
from src.strategies.liquidation_cascade import scan_liquidation_cascade
from src.strategies.fear_greed_contrarian import (
    scan_fear_greed_contrarian,
    on_position_opened as fgi_on_position_opened,
    on_position_closed as fgi_on_position_closed,
)
from src.strategies.correlation_break import scan_correlation_break
from src.strategies.cross_exchange_divergence import scan_cross_exchange_divergence
from src.strategies.narrative_momentum import scan_narrative_momentum
from src.strategies.protocol_revenue import scan_protocol_revenue, ProtocolMetrics
from src.indicators.core import (
    push_tick as push_indicator_tick, compute_atr_stop, compute_atr_trailing_stop,
    get_atr, _aggregate_to_htf,
)
from src.indicators.cvd import push_trade as push_cvd_trade, get_cvd_snapshot
from src.indicators.regime import classify_regime
from src.types import ScannerConfig, MarketContext, Position, TradeSignal
from src.risk.loss_cooldown import is_on_cooldown, record_trade_result
from src.risk.regime_gate import is_regime_blocked
from src.risk.scaling import get_initial_fraction, get_max_tranches, should_add_tranche, compute_tranche_size_usd
from src.risk.adaptive_stops import compute_adaptive_stop
from src.risk.signal_aggregator import SignalAggregator
from src.storage.database import insert_trade_journal
from src.evaluation.metrics import monte_carlo_significance

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
_WARMUP_PERIOD_S = 60  # 1 minute — collect data before trading
_last_mc_run = [0.0]  # mutable container for last Monte Carlo run timestamp

# Graceful shutdown event — background threads check this to exit promptly
_shutdown_event = threading.Event()

# ─── Shared state (thread-safe) ──────────────────────────────────────────────
_market_ctx_lock = threading.Lock()
_market_ctx: MarketContext = MarketContext(
    phase="neutral", btc_dominance=50.0, fear_greed_index=50,
    total_market_cap_change_d1=0, timestamp=0,
)

_config_lock = threading.Lock()

_price_lock = threading.Lock()
_latest_prices: dict[str, float] = {}  # symbol -> latest price
_prev_prices: dict[str, float] = {}    # symbol -> previous tick price (for CVD side inference)
_tick_count: dict[str, int] = {}       # symbol -> tick count for diagnostics
_last_diag_time = 0.0
_DIAG_INTERVAL_S = 60  # log diagnostics every 60 seconds
_last_convex_sync_time = 0.0
_CONVEX_SYNC_INTERVAL_S = 10  # sync position prices to Convex every 10 seconds
_ws_instance: Optional[CoinbaseWebSocket] = None  # for health check access

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

# Cross-strategy signal aggregation — deduplicates and boosts agreeing signals
_signal_aggregator = SignalAggregator(window_ms=3000)


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

    # Leverage profiles (separate from derivatives to avoid hot-path bloat)
    for sym in symbols[:10]:
        try:
            fetch_leverage_profile(sym)
        except Exception as err:
            log("error", f"Leverage profile fetch failed for {sym}: {err}")


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
            aggregated = _signal_aggregator.submit(sig)
            for agg_signal in aggregated:
                _process_signal(agg_signal, ctx)


# ─── Signal processing (qualify -> risk -> size -> execute) ───────────────────

def _process_signal(signal: TradeSignal, ctx: MarketContext) -> None:
    """Qualify a signal and open a position if it passes all checks."""
    # Validate required signal fields
    if not signal.symbol or not signal.product_id or signal.entry_price <= 0:
        log("error", f"Invalid signal — missing fields: symbol={signal.symbol} "
            f"product_id={signal.product_id} entry_price={signal.entry_price}")
        return

    # Snapshot config to avoid TOCTOU races with healer/analyzer threads
    with _config_lock:
        cfg = dataclasses.replace(config)

    # Check if strategy is enabled
    if not strategy_selector.is_strategy_enabled(signal.strategy):
        log("info", f"Signal blocked (strategy disabled): {signal.strategy} {signal.symbol}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Warm-up period: don't trade until we have enough data
    remaining = _WARMUP_PERIOD_S - (time.time() - _start_time)
    if remaining > 0:
        log("info", f"Signal blocked (warm-up {remaining:.0f}s left): {signal.strategy} {signal.symbol}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Consecutive loss cooldown check
    if is_on_cooldown(signal.strategy):
        log("info", f"Signal blocked (loss cooldown): {signal.strategy} {signal.symbol}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Regime-based hard gating: completely block strategy in wrong regime
    if is_regime_blocked(signal.strategy, signal.symbol):
        log("info", f"Signal blocked (regime gate): {signal.strategy} {signal.symbol}",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Signal staleness check: reject signals older than 30s (price may have moved)
    now_ms = time.time() * 1000
    if signal.created_at > 0:
        age_s = (now_ms - signal.created_at) / 1000
        if age_s > 30:
            log("info", f"Stale signal rejected ({age_s:.0f}s old): {signal.strategy} {signal.symbol}",
                symbol=signal.symbol, strategy=signal.strategy)
            return

    # Signal age decay: reduce score linearly over 30s (fresh=100%, 15s=75%, 30s=50%)
    if signal.created_at > 0:
        age_s = (now_ms - signal.created_at) / 1000
        decay_factor = max(0.5, 1.0 - (age_s / 60.0))  # linear decay, floor at 50%
        signal = dataclasses.replace(signal, score=signal.score * decay_factor)

    # Price sanity check: verify signal entry price is close to latest price
    with _price_lock:
        latest = _latest_prices.get(signal.symbol)
    if latest and signal.entry_price > 0:
        deviation = abs(signal.entry_price - latest) / latest
        if deviation > 0.02:  # >2% deviation = price has moved too far
            log("info", f"Signal price stale ({deviation:.1%} off): {signal.strategy} {signal.symbol}",
                symbol=signal.symbol, strategy=signal.strategy)
            return

    # Liquidity check: reject signals when bid-ask spread is too wide (>0.5%)
    spread = get_bid_ask_spread(signal.symbol)
    if spread is not None and spread > 0.005:
        log("info", f"Signal rejected — illiquid spread ({spread:.2%}): {signal.strategy} {signal.symbol}",
            symbol=signal.symbol, strategy=signal.strategy)
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
        signal, ctx, cfg,
        news=news, social=social, cvd=cvd, regime=regime,
        options=options, derivatives=derivatives,
        stablecoin=stablecoin, has_unlock_risk=has_unlock_risk,
    )
    if not qual.passed:
        try:
            log("info", f"Signal blocked (qual {qual.score:.0f} < min): {signal.strategy} {signal.symbol} — {qual.reasoning}",
                symbol=signal.symbol, strategy=signal.strategy)
        except (TypeError, AttributeError):
            pass
        return

    log("signal",
        f"{signal.strategy} signal: {signal.symbol} {signal.side} (qual={qual.score:.0f}) — {signal.reasoning}",
        symbol=signal.symbol, strategy=signal.strategy,
        data={"qual_score": qual.score, "breakdown": qual.breakdown})

    # Block duplicate: don't open same symbol+strategy if already open
    open_pos = get_open_positions()
    for pos in open_pos:
        if pos.symbol == signal.symbol and pos.strategy == signal.strategy:
            return

    # Risk check
    if not can_open_position():
        log("info", f"Risk manager blocked position for {signal.symbol}", symbol=signal.symbol)
        return

    # Position sizing
    portfolio_usd = get_paper_balance() if env.paper_trading else env.portfolio_usd
    size_usd = kelly_size(signal.strategy, portfolio_usd, qual.score)
    if size_usd <= 0:
        log("info", f"Kelly sizing returned 0 for {signal.strategy}", symbol=signal.symbol)
        return

    # Correlation-aware discount: reduce size when stacking correlated assets
    size_usd = apply_correlation_discount(size_usd, signal.symbol, signal.side, open_pos)

    # Sector exposure cap: don't exceed 30% portfolio in one group
    size_usd = check_sector_exposure(signal.symbol, signal.side, size_usd, portfolio_usd, open_pos)
    if size_usd <= 0:
        log("info", f"Sector exposure cap reached for {signal.symbol}", symbol=signal.symbol)
        return

    # Proactive regime scaling: adjust size based on current volatility
    from src.risk.regime_scaler import get_regime_scaling
    regime_scaling = get_regime_scaling(signal.symbol)
    size_usd *= regime_scaling.size_multiplier

    # DCA: swing positions enter with initial tranche fraction only
    initial_fraction = get_initial_fraction(signal.tier)
    full_size_usd = size_usd
    size_usd = size_usd * initial_fraction

    # Determine trail and hold time based on tier
    if signal.tier == "scalp":
        trail_pct = cfg.base_trail_pct_scalp
        max_hold_ms = cfg.max_hold_ms_scalp
    else:
        trail_pct = cfg.base_trail_pct_swing
        max_hold_ms = cfg.max_hold_ms_swing

    # Execute
    now = time.time() * 1000
    position_id = str(uuid.uuid4())

    try:
        trade = execute_buy(
            signal.symbol, signal.product_id, size_usd,
            position_id, signal.entry_price,
        )
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

    # Guard against zero/negative quantity positions (e.g., size below minimum)
    if quantity <= 0:
        log("error", f"Trade returned zero quantity for {signal.symbol}, skipping position",
            symbol=signal.symbol, strategy=signal.strategy)
        return

    # Compute ATR-based stop price (falls back to fixed % if ATR unavailable)
    stop_price, trail_pct = compute_atr_stop(
        signal.symbol, entry_price, signal.side, signal.strategy,
        fallback_trail_pct=trail_pct,
    )

    # Adaptive stop: use historical MAE if available (may widen the ATR stop)
    adaptive_trail = compute_adaptive_stop(signal.strategy, trail_pct)
    if adaptive_trail != trail_pct:
        # Use the wider of ATR stop and adaptive stop to give trades breathing room
        trail_pct = max(trail_pct, adaptive_trail)
        if signal.side == "long":
            stop_price = entry_price * (1 - trail_pct)
        else:
            stop_price = entry_price * (1 + trail_pct)

    # Proactive regime scaling: adjust stop distance based on current volatility
    trail_pct *= regime_scaling.stop_multiplier
    trail_pct = min(trail_pct, cfg.max_trail_pct)
    if signal.side == "long":
        stop_price = entry_price * (1 - trail_pct)
    else:
        stop_price = entry_price * (1 + trail_pct)

    # Capture momentum at entry for self-healing diagnosis
    from src.strategies.momentum import get_swing_buffer
    _momentum_at_entry = 0.0
    buf = get_swing_buffer(signal.symbol)
    if len(buf) >= 2 and buf[0].price > 0:
        _momentum_at_entry = (buf[-1].price - buf[0].price) / buf[0].price

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
        tranche_count=1,
        max_tranches=get_max_tranches(signal.tier),
        avg_entry_price=entry_price,
        entry_size_usd=trade.size_usd,
        total_commission=trade.commission,
        initial_stop_price=stop_price,
        momentum_at_entry=_momentum_at_entry,
    )

    # Persist and register
    with batch_writes():
        insert_position(position)
        insert_trade(trade)
    register_open(position)
    if signal.strategy == "fear_greed_contrarian":
        fgi_on_position_opened(signal.symbol)

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


# ─── Diagnostics ──────────────────────────────────────────────────────────────

def _log_diagnostics() -> None:
    """Log system diagnostics every 60s to help debug trading pipeline."""
    from src.strategies.momentum import get_ready_symbols
    from src.indicators.core import get_snapshot, get_atr

    uptime = time.time() - _start_time
    warmup_remaining = max(0, _WARMUP_PERIOD_S - uptime)

    with _price_lock:
        n_prices = len(_latest_prices)
        symbols_with_prices = list(_latest_prices.keys())[:5]
        total_ticks = sum(_tick_count.values())
        n_tick_symbols = len(_tick_count)

    # Buffer status for momentum
    swing_ready, scalp_ready = get_ready_symbols(min_samples=5)

    # Indicator readiness (sample BTC)
    btc_snap = get_snapshot("BTC")
    btc_atr = get_atr("BTC")
    btc_indicators = "none"
    if btc_snap:
        parts = []
        if btc_snap.rsi_14 is not None:
            parts.append(f"RSI={btc_snap.rsi_14:.0f}")
        if btc_snap.adx is not None:
            parts.append(f"ADX={btc_snap.adx:.0f}")
        if btc_snap.ema_20 is not None:
            parts.append("EMA20")
        if btc_snap.bb_width is not None:
            parts.append(f"BBW={btc_snap.bb_width:.3f}")
        btc_indicators = ", ".join(parts) if parts else "computing..."

    # Regime
    regime = classify_regime("BTC")

    # Market context
    with _market_ctx_lock:
        ctx = _market_ctx

    open_pos = get_open_positions()

    log("info",
        f"DIAGNOSTICS: uptime={uptime:.0f}s | warmup_left={warmup_remaining:.0f}s | "
        f"ticks={total_ticks} ({n_tick_symbols} symbols) | "
        f"prices={n_prices} [{', '.join(symbols_with_prices)}...] | "
        f"swing_ready={len(swing_ready)} scalp_ready={len(scalp_ready)} | "
        f"BTC: ATR={'%.2f' % btc_atr if btc_atr else 'None'} indicators=[{btc_indicators}] | "
        f"regime: trend={regime.trend} vol={regime.volatility} phase={regime.phase} score={regime.regime_score:.0f} | "
        f"market: phase={ctx.phase} FGI={ctx.fear_greed_index:.0f} | "
        f"positions: {len(open_pos)} open")


# ─── Tick-driven strategy scanning ───────────────────────────────────────────

def _on_tick(symbol: str, price: float, volume: float) -> None:
    """Called on each WebSocket tick. Feed data to strategies and scan."""
    # Price feed validation: reject unreasonable prices
    if price <= 0.001:
        return  # absolute floor — no sub-penny assets

    global _last_diag_time
    now_diag = time.time()

    with _price_lock:
        prev = _latest_prices.get(symbol)
        if prev and prev > 0:
            change = abs(price - prev) / prev
            if change > 0.20:
                log("warn", f"Price spike rejected: {symbol} {prev:.4f} -> {price:.4f} ({change:.1%})",
                    symbol=symbol)
                return  # reject >20% single-tick moves as feed glitches
        _latest_prices[symbol] = price
        _tick_count[symbol] = _tick_count.get(symbol, 0) + 1
        should_diag = now_diag - _last_diag_time >= _DIAG_INTERVAL_S
        if should_diag:
            _last_diag_time = now_diag
    if should_diag:
        _log_diagnostics()

    # Update open position prices
    for pos in get_open_positions():
        if pos.symbol == symbol:
            update_position_price(pos.id, price)

    # Periodically sync position prices to Convex for dashboard
    global _last_convex_sync_time
    now_sync = time.time()
    with _price_lock:
        should_sync = now_sync - _last_convex_sync_time >= _CONVEX_SYNC_INTERVAL_S
        if should_sync:
            _last_convex_sync_time = now_sync
    # Sync outside _price_lock to avoid lock-ordering deadlock with portfolio._lock
    if should_sync:
        for pos in get_open_positions():
            db_update_position_price(
                pos.id, pos.current_price,
                pos.high_watermark, pos.low_watermark,
                pos.stop_price, pos.quantity,
            )

    # Feed price samples to tick-driven strategies and indicator engine
    push_price_sample(symbol, price, volume)
    push_ohlcv_sample(symbol, price, volume)
    push_indicator_tick(symbol, price, volume)

    # Infer trade side from price movement for CVD
    with _price_lock:
        prev_price = _prev_prices.get(symbol, price)
        _prev_prices[symbol] = price
    inferred_side = "buy" if price >= prev_price else "sell"
    push_cvd_trade(symbol, price, volume, inferred_side)

    # Periodically aggregate minute candles to higher timeframes
    now_s = time.time()
    with _price_lock:
        last_agg = _last_htf_aggregate.get(symbol, 0)
        should_agg = now_s - last_agg >= _HTF_AGGREGATE_INTERVAL_S
        if should_agg:
            _last_htf_aggregate[symbol] = now_s
    if should_agg:
        _aggregate_to_htf(symbol)

    # Throttle scanning
    now = time.time()
    with _price_lock:
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

    # 9. Cross-exchange divergence (Coinbase vs Binance price dislocation)
    _try_scan(lambda: scan_cross_exchange_divergence(symbol, product_id, price, config, ctx), ctx)


def _try_scan(scan_fn, ctx: MarketContext) -> None:
    """Execute a scan function, process any signal returned."""
    try:
        sig = scan_fn()
        if sig:
            aggregated = _signal_aggregator.submit(sig)
            for agg_signal in aggregated:
                _process_signal(agg_signal, ctx)
    except Exception as err:
        import traceback
        log("error", f"Strategy scan error: {err}", data={"traceback": traceback.format_exc()[-500:]})


def _try_scan_correlation(symbol: str, product_id: str, price: float, ctx: MarketContext) -> None:
    """Scan correlation break strategy using BTC as reference."""
    from src.strategies.momentum import get_swing_buffer
    if symbol == "BTC":
        return
    with _price_lock:
        btc_price = _latest_prices.get("BTC")
    if not btc_price:
        return
    # Compute actual 1h % change from swing buffers
    btc_buf = get_swing_buffer("BTC")
    alt_buf = get_swing_buffer(symbol)
    btc_1h_pct = 0.0
    alt_1h_pct = 0.0
    if len(btc_buf) >= 2 and btc_buf[0].price > 0:
        btc_1h_pct = (btc_buf[-1].price - btc_buf[0].price) / btc_buf[0].price
    if len(alt_buf) >= 2 and alt_buf[0].price > 0:
        alt_1h_pct = (alt_buf[-1].price - alt_buf[0].price) / alt_buf[0].price
    try:
        sig = scan_correlation_break(
            symbol, product_id, price,
            btc_1h_pct=btc_1h_pct, alt_1h_pct=alt_1h_pct,
            config=config, ctx=ctx,
        )
        if sig:
            aggregated = _signal_aggregator.submit(sig)
            for agg_signal in aggregated:
                _process_signal(agg_signal, ctx)
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
        sig = scan_narrative_momentum(product_id_map, config, current_prices, ctx=ctx)
        if sig:
            aggregated = _signal_aggregator.submit(sig)
            for agg_signal in aggregated:
                _process_signal(agg_signal, ctx)
    except Exception as err:
        log("error", f"Narrative momentum scan error: {err}")


# ─── Order book updates ──────────────────────────────────────────────────────

def _on_book(symbol: str, bids: list, asks: list) -> None:
    """Called on each WebSocket L2 update."""
    try:
        # Validate book data: reject negative sizes and inverted books
        bid_levels = [
            OrderBookLevel(price=b["price"], size=b["size"])
            for b in bids if b.get("size", 0) >= 0 and b.get("price", 0) > 0
        ]
        ask_levels = [
            OrderBookLevel(price=a["price"], size=a["size"])
            for a in asks if a.get("size", 0) >= 0 and a.get("price", 0) > 0
        ]
        # Sanity check: best bid should be below best ask
        if bid_levels and ask_levels and bid_levels[0].price >= ask_levels[0].price:
            return  # inverted book, likely stale data
        update_order_book(symbol, bid_levels, ask_levels)
    except Exception as err:
        log("error", f"Order book update error for {symbol}: {err}")


# ─── Exit checking ────────────────────────────────────────────────────────────


def _compute_r_multiple(pos: Position, current_price: float) -> float:
    """Compute R-multiple: how many R (risk units) the trade has moved in our favor.

    R = distance from entry to initial stop (frozen at open). R-multiple = profit / R.
    """
    if pos.entry_price <= 0:
        return 0.0
    # Use initial_stop_price (frozen at open) for consistent R calculation
    stop_ref = pos.initial_stop_price if pos.initial_stop_price > 0 else pos.stop_price
    initial_risk = abs(pos.entry_price - stop_ref) if stop_ref > 0 else pos.entry_price * pos.trail_pct
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
        trade = execute_sell(pos.symbol, pos.product_id, partial_qty, pos.id, current_price)
    except Exception as err:
        log("warn", f"Partial exit failed for {pos.symbol}: {err}",
            symbol=pos.symbol, strategy=pos.strategy)
        return

    if trade.status == "failed":
        return

    # Track the partial exit
    if pos.original_quantity is None:
        pos.original_quantity = pos.quantity

    # Compute partial P&L before updating quantity
    if pos.entry_price > 0:
        if pos.side == "long":
            partial_pnl_pct = (trade.price - pos.avg_entry_price) / pos.avg_entry_price
        else:
            partial_pnl_pct = (pos.avg_entry_price - trade.price) / pos.avg_entry_price
        # Use entry-basis size for this partial chunk
        partial_chunk_entry_usd = (partial_qty / pos.original_quantity) * pos.entry_size_usd
        partial_pnl_usd = partial_pnl_pct * partial_chunk_entry_usd - trade.commission
        pos.partial_realized_pnl += partial_pnl_usd
    else:
        partial_pnl_usd = 0.0

    pos.quantity -= partial_qty
    pos.total_commission += trade.commission
    # Fix P0.3: Track fraction of original quantity sold, not raw accumulation
    if pos.original_quantity and pos.original_quantity > 0:
        pos.partial_exit_pct = 1.0 - (pos.quantity / pos.original_quantity)
    # Keep size_usd on entry basis for remaining quantity
    if pos.original_quantity and pos.original_quantity > 0:
        pos.size_usd = (pos.quantity / pos.original_quantity) * pos.entry_size_usd

    insert_trade(trade)

    # Register partial P&L with risk manager for daily loss tracking (P1.4)
    register_close(pos, partial_pnl_usd, is_partial=True)

    r_mult = _compute_r_multiple(pos, current_price)
    log("trade",
        f"PARTIAL EXIT {pos.side.upper()} {pos.symbol} — sold {fraction*100:.0f}% "
        f"@ {current_price:.4f} (R={r_mult:.1f} partial_pnl=${partial_pnl_usd:.2f}), trailing remainder",
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

    # Regime-aware stop adjustment: widen in high vol, tighten in low vol
    try:
        regime = classify_regime(pos.symbol)
        if regime.volatility == "high_vol":
            # High vol: widen stops (use larger trail %) to avoid noise whipsaws
            wider_trail = pos.trail_pct * 1.2
            if pos.side == "long":
                widened = pos.high_watermark * (1 - wider_trail)
                # Only update if this widens (lowers) the stop
                if widened < pos.stop_price:
                    pos.stop_price = widened
            else:
                widened = pos.low_watermark * (1 + wider_trail)
                if widened > pos.stop_price:
                    pos.stop_price = widened
        elif regime.volatility == "low_vol":
            # Low vol: tighten stops (use smaller trail %) — less room for noise needed
            tighter_trail = pos.trail_pct * 0.8
            if pos.side == "long":
                tightened = pos.high_watermark * (1 - tighter_trail)
                # Only update if this tightens (raises) the stop
                if tightened > pos.stop_price:
                    pos.stop_price = tightened
            else:
                tightened = pos.low_watermark * (1 + tighter_trail)
                if tightened < pos.stop_price:
                    pos.stop_price = tightened
    except Exception as err:
        log("warn", f"Regime stop adjustment failed: {err}", symbol=pos.symbol)

    # DCA scaling-in: check if we should add another tranche
    if pos.tranche_count < pos.max_tranches:
        tranche = should_add_tranche(pos, current_price)
        if tranche:
            try:
                tranche_usd = compute_tranche_size_usd(pos, tranche["fraction"])

                trade = execute_buy(pos.symbol, pos.product_id, tranche_usd, pos.id, current_price)

                if trade.status != "failed" and trade.quantity > 0:
                    # Update position with new tranche
                    old_qty = pos.quantity
                    pos.quantity += trade.quantity
                    pos.size_usd += trade.size_usd
                    pos.entry_size_usd += trade.size_usd  # track total entry cost
                    pos.total_commission += trade.commission
                    pos.tranche_count += 1
                    # Update average entry price using actual fill price, not current_price
                    pos.avg_entry_price = (
                        (pos.avg_entry_price * old_qty + trade.price * trade.quantity)
                        / pos.quantity
                    )
                    # Update original_quantity to reflect total DCA'd quantity
                    pos.original_quantity = pos.quantity
                    insert_trade(trade)
                    log("trade",
                        f"DCA TRANCHE {pos.tranche_count}/{pos.max_tranches} {pos.symbol} "
                        f"+${trade.size_usd:.0f} @ {current_price:.4f} ({tranche['reason']})",
                        symbol=pos.symbol, strategy=pos.strategy)
            except Exception as err:
                log("warn", f"DCA tranche failed for {pos.symbol}: {err}",
                    symbol=pos.symbol, strategy=pos.strategy)

    # Breakeven stop: once trade reaches 1R profit, move stop to entry price
    if pos.entry_price > 0:
        r_multiple = _compute_r_multiple(pos, current_price)
        if r_multiple >= 1.0:
            if pos.side == "long" and pos.stop_price < pos.entry_price:
                pos.stop_price = pos.entry_price
            elif pos.side == "short" and pos.stop_price > pos.entry_price:
                pos.stop_price = pos.entry_price

    # Partial take-profit: sell 50% at 1.5R, trail the rest (only if no prior partial)
    if pos.partial_exit_pct < 0.01 and pos.entry_price > 0:
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price if pos.side == "long" \
            else (pos.entry_price - current_price) / pos.entry_price
        r_multiple = _compute_r_multiple(pos, current_price)
        if r_multiple >= 1.5 and pnl_pct > 0.015:
            _execute_partial_exit(pos, current_price, now, fraction=0.5)

    # Stale position tightening: if >50% of max hold and MFE < 1%,
    # tighten trailing stop by 30% to encourage exit
    hold_elapsed = now - pos.opened_at
    if pos.max_hold_ms > 0 and hold_elapsed > pos.max_hold_ms * 0.5:
        if pos.mfe_pct < 0.01:  # hasn't moved 1% favorably
            tightened = pos.trail_pct * 0.7
            if pos.side == "long":
                tight_stop = pos.high_watermark * (1 - tightened)
                if tight_stop > pos.stop_price:
                    pos.stop_price = tight_stop
            else:
                tight_stop = pos.low_watermark * (1 + tightened)
                if tight_stop < pos.stop_price:
                    pos.stop_price = tight_stop

    # Determine exit reason
    exit_reason = None

    # 1. Trailing stop hit
    if pos.side == "long" and current_price <= pos.stop_price:
        exit_reason = "trailing_stop"
    elif pos.side == "short" and current_price >= pos.stop_price:
        exit_reason = "trailing_stop"

    # 2. Max hold time exceeded
    hold_ms = now - pos.opened_at
    if pos.max_hold_ms > 0 and hold_ms >= pos.max_hold_ms:
        exit_reason = "time_limit"

    if not exit_reason:
        return

    # Execute sell
    try:
        trade = execute_sell(
            pos.symbol, pos.product_id, pos.quantity,
            pos.id, current_price,
        )
    except Exception as err:
        log("error", f"Exit execution failed for {pos.symbol}: {err}",
            symbol=pos.symbol, strategy=pos.strategy)
        return

    if trade.status == "failed":
        log("error", f"Exit trade failed for {pos.symbol}: {trade.error}",
            symbol=pos.symbol, strategy=pos.strategy)
        # If no holdings exist, force-close the stale position to stop retry loop
        if "No holdings" in (trade.error or ""):
            # Compute actual P&L from entry price vs current price
            if pos.side == "long":
                fc_pnl_pct = (current_price - pos.avg_entry_price) / pos.avg_entry_price if pos.avg_entry_price > 0 else 0
            else:
                fc_pnl_pct = (pos.avg_entry_price - current_price) / pos.avg_entry_price if pos.avg_entry_price > 0 else 0
            entry_basis = pos.entry_size_usd if pos.entry_size_usd > 0 else pos.size_usd
            fc_pnl_usd = pos.partial_realized_pnl + (fc_pnl_pct * pos.size_usd)
            fc_pnl_pct_total = fc_pnl_usd / entry_basis if entry_basis > 0 else fc_pnl_pct

            log("warn", f"Force-closing stale position {pos.symbol} — no holdings to sell "
                f"(entry={pos.avg_entry_price:.6f} exit={current_price:.6f} pnl={fc_pnl_pct_total:.2%})",
                symbol=pos.symbol, strategy=pos.strategy)
            pos.status = "closed"
            pos.exit_price = current_price
            pos.closed_at = now
            pos.pnl_usd = fc_pnl_usd
            pos.pnl_pct = fc_pnl_pct_total
            pos.exit_reason = "force_closed_no_holdings"
            with batch_writes():
                update_position_close(pos.id, current_price, fc_pnl_usd, fc_pnl_pct_total, "force_closed_no_holdings")
            register_close(pos, fc_pnl_usd)
        return

    exit_price = trade.price if trade.price > 0 else current_price

    # Track exit commission
    pos.total_commission += trade.commission

    # Compute PnL for remaining position (entry-basis)
    if pos.side == "long":
        remaining_pnl_pct = (exit_price - pos.avg_entry_price) / pos.avg_entry_price if pos.avg_entry_price > 0 else 0
    else:
        remaining_pnl_pct = (pos.avg_entry_price - exit_price) / pos.avg_entry_price if pos.avg_entry_price > 0 else 0
    # remaining_size_usd is already on entry basis after partial exit fixes
    remaining_pnl_usd = remaining_pnl_pct * pos.size_usd - trade.commission

    # Total P&L = partial exits + remaining - commissions on entry
    pnl_usd = pos.partial_realized_pnl + remaining_pnl_usd
    # Overall pnl_pct relative to original entry size
    entry_basis = pos.entry_size_usd if pos.entry_size_usd > 0 else pos.size_usd
    pnl_pct = pnl_usd / entry_basis if entry_basis > 0 else 0

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
    if pos.strategy == "fear_greed_contrarian":
        fgi_on_position_closed(pos.symbol)

    # Update peak portfolio for drawdown-based sizing
    portfolio_usd = get_paper_balance() if env.paper_trading else env.portfolio_usd
    update_peak(portfolio_usd)

    # Self-healing: diagnose the trade
    on_position_closed(pos, config, ctx.phase)

    # Track consecutive losses for cooldown
    record_trade_result(pos.strategy, pnl_pct >= 0)

    # Trade journal: structured exit analysis
    try:
        regime_now = classify_regime(pos.symbol)
        r_mult = _compute_r_multiple(pos, exit_price)
        hold_hours = (now - pos.opened_at) / 3_600_000
        was_partial_beneficial = (
            pos.partial_exit_pct > 0 and pos.pnl_pct is not None and pos.pnl_pct < 0
        )  # partial helped if we lost money (sold some earlier)
        insert_trade_journal({
            "id": str(uuid.uuid4()),
            "position_id": pos.id,
            "symbol": pos.symbol,
            "strategy": pos.strategy,
            "r_multiple": r_mult,
            "hold_hours": hold_hours,
            "mae_pct": pos.mae_pct,
            "mfe_pct": pos.mfe_pct,
            "partial_exit_pct": pos.partial_exit_pct,
            "exit_reason": exit_reason,
            "pnl_pct": pnl_pct,
            "regime_at_entry": "unknown",  # would need to store at entry time
            "regime_at_exit": f"{regime_now.trend}/{regime_now.volatility}",
            "was_partial_beneficial": 1 if was_partial_beneficial else 0,
            "timestamp": now,
        })
    except Exception as err:
        log("warn", f"Trade journal insert failed: {err}")

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

            # Monte Carlo significance test (at most once per hour)
            if time.time() - _last_mc_run[0] >= 3600:
                _last_mc_run[0] = time.time()
                try:
                    sig_results = monte_carlo_significance(num_simulations=2000)
                    for sr in sig_results:
                        if not sr.significant and sr.num_trades >= 30:
                            log("warn",
                                f"Strategy {sr.strategy} NOT significant: "
                                f"Sharpe={sr.actual_sharpe:.2f}, p={sr.p_value:.3f}, "
                                f"trades={sr.num_trades}",
                                strategy=sr.strategy)
                        elif sr.significant:
                            log("info",
                                f"Strategy {sr.strategy} significant: "
                                f"Sharpe={sr.actual_sharpe:.2f}, p={sr.p_value:.3f}",
                                strategy=sr.strategy)
                except Exception as mc_err:
                    log("warn", f"Monte Carlo test failed: {mc_err}")

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
    ws_check_counter = 0
    while not _shutdown_event.is_set():
        if _shutdown_event.wait(timeout=_SCALP_EXIT_CHECK_INTERVAL_S):
            break
        try:
            # Check WS health every ~10 seconds
            ws_check_counter += 1
            if ws_check_counter >= 10 and _ws_instance:
                ws_check_counter = 0
                _ws_instance.check_health(max_silence_s=30.0)

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


_HEALTH_TOKEN = os.environ.get("HEALTH_TOKEN", "")


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for Railway health checks."""

    def do_GET(self):
        if self.path == "/health":
            # Require auth token if configured
            if _HEALTH_TOKEN:
                auth = self.headers.get("Authorization", "")
                if not _hmac_mod.compare_digest(auth, f"Bearer {_HEALTH_TOKEN}"):
                    self.send_response(401)
                    self.end_headers()
                    return
            else:
                # No token configured — return minimal response to avoid leaking state
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "healthy"}).encode())
                return

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
            except Exception as exc:
                log("warn", f"Health check: failed to compute daily PnL: {type(exc).__name__}")

            # Last trade timestamp
            last_trade_at = 0
            try:
                recent = get_closed_trades(1)
                if recent and hasattr(recent[0], "closed_at") and recent[0].closed_at:
                    last_trade_at = int(recent[0].closed_at)
            except Exception as exc:
                log("warn", f"Health check: failed to fetch last trade: {type(exc).__name__}")

            # Portfolio metrics
            sharpe = compute_sharpe()
            max_dd = compute_max_drawdown()
            cvar = compute_cvar()

            status = {
                "status": "healthy",
                "uptime_seconds": round(time.time() - _start_time),
                "paper_trading": env.paper_trading,
                "open_positions": len(open_positions),
                "strategies_count": len(registry),
                "last_trade_at": last_trade_at,
                "daily_pnl": round(daily_pnl, 2),
                "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
                "max_drawdown_pct": round(max_dd * 100, 1),
                "cvar_95_daily": round(cvar, 2) if cvar is not None else None,
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
    if not _HEALTH_TOKEN:
        log("warn", "HEALTH_TOKEN not set — health endpoint exposes system state without auth")
    port = int(os.environ.get("PORT", "8080"))
    server = http.server.HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health", daemon=True)
    thread.start()
    log("info", f"Health check server started on port {port}")


# ─── Main entry point ────────────────────────────────────────────────────────

def main() -> None:
    print(f"[STARTUP] paper_trading={env.paper_trading} convex_url={env.convex_url} portfolio={env.portfolio_usd}", flush=True)
    # ── Initialize Convex database (must be first — everything else calls log()) ──
    if not env.convex_url:
        print("[FATAL] CONVEX_URL not set. Set it in your .env file.")
        sys.exit(1)
    _db_mod.init(env.convex_url)
    log("info", "Database: Convex initialized")

    # Initialize declarative protection chain
    init_protections(DEFAULT_PROTECTIONS)

    log("info", "--- Self-Healing Crypto Trader starting ---",
        data={
            "paper_trading": env.paper_trading,
            "portfolio_usd": env.portfolio_usd,
            "max_position_usd": env.max_position_usd,
            "max_open_positions": env.max_open_positions,
            "log_analysis_interval_mins": env.log_analysis_interval_mins,
        })

    if env.paper_trading:
        log("info", "PAPER TRADING mode — no real orders will be placed")

    if not env.anthropic_api_key:
        log("warn", "ANTHROPIC_API_KEY not set — Claude log analysis disabled")

    # Required API keys
    _missing_keys = []
    if not env.coinbase_api_key:
        _missing_keys.append("COINBASE_API_KEY")
    if _missing_keys:
        log("error", f"Missing required API keys: {', '.join(_missing_keys)}")
        sys.exit(1)

    # Optional API keys — features degrade gracefully without them
    if not env.lunarcrush_api_key:
        log("warn", "LUNARCRUSH_API_KEY not set — social signals disabled, narrative_momentum strategy will not fire")
    if not env.binance_api_key:
        log("warn", "BINANCE_API_KEY not set — Binance signals, derivatives, and cross-exchange strategy disabled")

    # CONFIG_BOUNDS validation at startup
    violations = validate_config(config)
    if violations:
        log("warn", f"Config bounds violations at startup: {'; '.join(violations)}")
    else:
        log("info", "Config bounds validation passed")

    # Log Kelly sizing rationale for each strategy at startup
    registry = get_registry()
    for entry in registry.values():
        log("info", log_kelly_rationale(entry.strategy_id))

    # Initialize portfolio peak with configured portfolio size
    portfolio_usd = get_paper_balance() if env.paper_trading else env.portfolio_usd
    update_peak(portfolio_usd)

    # ── Restore open positions from Convex into in-memory tracking ──────────
    # On restart, load existing open positions so the bot continues managing them
    # (trailing stops, exit checks, dedup) instead of losing track of them.
    try:
        existing_open = _db_mod.get_open_positions()
        for pos in existing_open:
            register_open(pos)
            if pos.strategy == "fear_greed_contrarian":
                fgi_on_position_opened(pos.symbol)
        if existing_open:
            log("info", f"Restored {len(existing_open)} open positions into in-memory tracking",
                data={"symbols": [p.symbol for p in existing_open]})
        else:
            log("info", "No open positions to restore")
    except Exception as e:
        log("warn", f"Failed to restore open positions: {e}")

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
    global _ws_instance
    ws = CoinbaseWebSocket(
        product_ids=DEFAULT_WATCHLIST,
        on_tick=_on_tick,
        on_book=_on_book,
    )
    _ws_instance = ws
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
