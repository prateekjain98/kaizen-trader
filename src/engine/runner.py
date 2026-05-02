"""Real-time trading engine runner.

Architecture:
    DataStreams → SignalDetector → ClaudeBrain (every 60s) → Executor

    Every 60 seconds, Claude sees:
    - All open positions with live P&L
    - All new signals since last tick
    - Market regime (FGI, funding, trending)
    - And decides: BUY / CLOSE / do nothing

Usage:
    python -m src.engine.runner              # paper trading
    python -m src.engine.runner --live       # live Binance execution
    python -m src.engine.runner --tick 30    # 30 second tick interval
"""

import argparse
import hashlib
import hmac
import os
import signal
import sys
import threading
import time
from collections import deque
from urllib.request import urlopen, Request

from src.engine.acceleration_tracker import AccelerationTracker
from src.engine.brain_memory import BrainMemory
from src.engine.data_streams import DataStreams, TokenSignal, fetch_binance_prices, fetch_binance_top_movers
from src.engine.signal_detector import SignalDetector, SignalPacket
from src.engine.claude_brain import ClaudeBrain
from src.engine.executor import Executor, _PORTFOLIO_FILE
from src.engine.correlation_scanner import CorrelationScanner
from src.engine.log import log

def _fetch_binance_account_balance() -> float | None:
    """Fetch USDT balance from the configured exchange account.

    Supports Binance Futures and OKX. Checks EXCHANGE env var to decide.
    """
    import json as _json
    from src.config import env as _env

    if _env.exchange == "okx":
        # Use OKX balance endpoint
        if not _env.okx_api_key or not _env.okx_api_secret:
            log("warn", "OKX_API_KEY / OKX_API_SECRET not set — cannot fetch balance")
            return None
        try:
            from src.execution.providers import OKXProvider
            provider = OKXProvider()
            balances = provider.get_balances()
            return balances.get("USDT", None)
        except Exception as e:
            log("error", f"Failed to fetch OKX balance: {e}")
            return None

    # Default: Binance
    api_key = os.environ.get("BINANCE_API_KEY")
    api_secret = os.environ.get("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        log("warn", "BINANCE_API_KEY / BINANCE_API_SECRET not set — cannot fetch balance")
        return None
    try:
        ts = int(time.time() * 1000)
        params = f"timestamp={ts}"
        signature = hmac.new(api_secret.encode(), params.encode(), hashlib.sha256).hexdigest()
        url = f"https://fapi.binance.com/fapi/v2/balance?{params}&signature={signature}"
        req = Request(url, headers={"X-MBX-APIKEY": api_key})
        with urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        for asset in data:
            if asset.get("asset") == "USDT":
                # Bug class: `balance` is wallet balance (includes margin
                # locked in open positions). Sizing must use availableBalance,
                # the actual free USDT we can deploy on a new entry.
                return float(asset.get("availableBalance", 0))
        return None
    except Exception as e:
        log("error", f"Failed to fetch Binance balance: {e}")
        return None


# Top symbols to scan for correlation breaks (from backtest)
_CORR_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "LINK", "AVAX",
    "UNI", "AAVE", "DOT", "MATIC", "FIL", "NEAR", "FTM", "ARB", "OP",
    "SUI", "APT", "SEI", "INJ", "RENDER", "FET", "PENDLE", "TAO",
    "PEPE", "BONK", "WIF", "DYDX", "GRT", "SNX", "COMP", "MKR",
]


class TradingEngine:
    """Real-time Claude-powered trading engine.

    Core loop (every tick_interval seconds):
        1. DataStreams feed signals into SignalDetector
        2. SignalDetector queues SignalPackets into ClaudeBrain
        3. ClaudeBrain.tick() — one Haiku call with FULL market state
        4. Returns list of TradeDecisions
        5. Executor processes each decision (open/close positions)
        6. Price updater checks stops/targets every 5s
        7. Correlation scanner runs every hour (197% CAGR strategy)

    Cost: ~$0.50/day at 60s ticks.
    """

    def __init__(self, paper: bool = True, balance: float = 10_000, tick_interval: int = 60,
                 trust_initial_balance: bool = False):
        self.paper = paper
        self.tick_interval = tick_interval
        self._stop = threading.Event()

        # Components
        self.detector = SignalDetector()

        # Use ClaudeBrain if API key available, otherwise fall back to RuleBrain
        if os.environ.get("ANTHROPIC_API_KEY"):
            self.brain = ClaudeBrain(balance=balance)
            log("info", "Brain: ClaudeBrain (Anthropic API)")
        else:
            from src.engine.rule_brain import RuleBrain
            self.brain = RuleBrain(balance=balance)
            log("info", "Brain: RuleBrain (no ANTHROPIC_API_KEY — zero API cost)")

        self.executor = Executor(paper=paper, initial_balance=balance,
                                 trust_initial_balance=trust_initial_balance)
        self.streams = DataStreams(on_signal=self._on_raw_signal)
        self.memory = BrainMemory()
        self.memory.load()
        self.accel_tracker = AccelerationTracker()
        self.corr_scanner = CorrelationScanner()

        # OKX WS: only meaningful in live mode against an OKX account.
        # Public WS gets dynamic ticker subs for open positions (sub-second stop checks).
        # Private WS pushes order/position/balance events (replaces REST polling latency).
        self.okx_public_ws = None
        self.okx_private_ws = None
        from src.config import env as _env
        if not paper and _env.exchange == "okx" and _env.okx_api_key:
            from src.engine.okx_ws import OKXPublicWS, OKXPrivateWS
            self.okx_public_ws = OKXPublicWS(on_ticker=self._on_okx_ticker)
            self.okx_private_ws = OKXPrivateWS(
                on_order=self._on_okx_order,
                on_position=self._on_okx_position,
                on_balance=self._on_okx_balance,
            )
        # Track which symbols we've subscribed for OKX public ticks, so we can
        # diff against current open positions each tick and sub/unsub accordingly.
        self._okx_subbed: set[str] = set()

        # Stats
        self.signals_received = 0
        self.ticks_run = 0
        self.trades_opened = 0
        self.trades_closed = 0

    def _on_raw_signal(self, signal: TokenSignal):
        """Called by DataStreams when a new signal arrives."""
        self.signals_received += 1

        # Convert to SignalPacket via rule-based detector
        packet = self.detector.process(signal, self.streams.snapshot)
        if packet:
            # Queue for next brain tick
            self.brain.add_signal(packet)
            # Pre-subscribe CVD tracker on high-priority signals so by the time
            # the brain decides to enter, we already have minutes of tape. No-op
            # in paper mode (tracker not started). subscribe() is idempotent.
            if not self.paper and getattr(packet, "priority", 0) >= 2:
                try:
                    from src.engine.cvd_tracker import get_tracker as _cvd
                    _cvd().subscribe(packet.symbol)
                except Exception:
                    pass

    # ── OKX WS callbacks ───────────────────────────────────────────────

    def _on_okx_ticker(self, symbol: str, last: float, bid: float, ask: float):
        """Real-time OKX price for an open position. Drives stop/target checks
        immediately instead of waiting for the 5s polled price update.

        Note: we still update the same executor.update_price path so trailing
        stops + thesis checks all run identically — just on fresher data.
        """
        if last > 0:
            self.executor.update_price(symbol, last)

    def _on_okx_order(self, order: dict):
        """OKX order state change pushed to us. Useful for fill confirmation
        without polling. Just log + delegate; the executor doesn't currently
        track order IDs in-process so we don't reconcile here.
        """
        state = order.get("state", "")
        inst = order.get("instId", "")
        if state in ("filled", "canceled", "partially_filled"):
            log("trade", f"OKX order push {inst} state={state} avgPx={order.get('avgPx')} "
                         f"fillSz={order.get('fillSz')}", symbol=inst.split("-")[0] if inst else None)

    def _on_okx_position(self, position: dict):
        """OKX position update. Currently advisory — executor maintains its own
        in-memory position state. If we later move to OKX-as-source-of-truth,
        this is the hook to reconcile.
        """
        pass

    def _on_okx_balance(self, usdt_avail: float):
        """OKX USDT availBal pushed to us. Refresh executor balance so brain
        decisions size against current truth instead of last polled value.
        """
        if usdt_avail > 0:
            self.executor.balance = usdt_avail

    def _sync_okx_subs(self):
        """Diff open positions against current OKX public-WS subscriptions
        and sub/unsub the delta. Called from the price-update loop each tick.
        """
        if not self.okx_public_ws:
            return
        wanted: set[str] = {p.symbol for p in self.executor.positions}
        new = wanted - self._okx_subbed
        gone = self._okx_subbed - wanted
        if new:
            self.okx_public_ws.subscribe(list(new))
        if gone:
            self.okx_public_ws.unsubscribe(list(gone))
        self._okx_subbed = wanted

    def _sync_brain_state(self):
        """Sync executor state into brain so Claude sees current portfolio."""
        # Tell DataStreams which symbols are currently held so the OBI-F
        # WS sub can rotate to (held ∪ trending). The L2 depth stream is
        # high-bandwidth — only subscribe where we actually trade.
        try:
            self.streams._held_position_symbols = {
                p.symbol for p in self.executor.positions
            }
        except Exception:
            pass
        self.brain.balance = self.executor.balance
        self.brain.daily_pnl = self.executor.daily_pnl
        self.brain.open_positions = [
            {
                "symbol": p.symbol, "side": p.side,
                "size_usd": p.size_usd, "entry": p.entry_price,
                "current_price": p.current_price,
                "pnl_pct": p.unrealized_pnl_pct * 100,
                "opened_at": p.opened_at,
            }
            for p in self.executor.positions
        ]

        # Sync market data from streams
        snapshot = self.streams.snapshot
        self.brain.fgi = snapshot.fear_greed_index
        self.brain.fgi_class = "Extreme Fear" if snapshot.fear_greed_index <= 20 else \
            "Extreme Greed" if snapshot.fear_greed_index >= 80 else \
            "Fear" if snapshot.fear_greed_index <= 40 else \
            "Greed" if snapshot.fear_greed_index >= 60 else "Neutral"
        self.brain.funding_rates = snapshot.funding_rates
        self.brain.trending_tokens = snapshot.trending_tokens
        self.brain.recent_listings = snapshot.recent_listings

        # Use cached social/news data from DataStreams (avoids blocking HTTP in brain tick)
        if hasattr(snapshot, 'reddit_posts'):
            reddit_posts = getattr(snapshot, 'reddit_posts', [])
            if reddit_posts:
                bullish = sum(1 for p in reddit_posts if any(kw in p.get('title', '').lower() for kw in ['bull', 'pump', 'rally', 'gain', 'moon', 'surge']))
                bearish = sum(1 for p in reddit_posts if any(kw in p.get('title', '').lower() for kw in ['bear', 'crash', 'dump', 'loss', 'tank']))
                sentiment = (bullish - bearish) / max(1, bullish + bearish) if (bullish + bearish) > 0 else 0
                self.brain.reddit_sentiment = sentiment
                self.brain.reddit_post_count = len(reddit_posts)
            else:
                self.brain.reddit_sentiment = 0
                self.brain.reddit_post_count = 0
        else:
            self.brain.reddit_sentiment = 0
            self.brain.reddit_post_count = 0

        if hasattr(snapshot, 'news_items'):
            news_items = getattr(snapshot, 'news_items', [])
            self.brain.latest_news = news_items[:5] if news_items else []
        else:
            self.brain.latest_news = []

        # Get 1h acceleration from WS-based tracker (all symbols, zero API calls)
        all_accel = self.accel_tracker.get_all_accelerations(min_abs_pct=1.0)
        for sig in self.brain.pending_signals:
            if sig.symbol in all_accel and sig.data is not None:
                sig.data["acceleration_1h"] = all_accel[sig.symbol]
        # Pass acceleration data to brain
        if hasattr(self.brain, 'acceleration_data'):
            self.brain.acceleration_data = all_accel
        top_accel = self.accel_tracker.get_top_accelerators(5)
        if top_accel:
            log("info", f"1h accel: {' | '.join(f'{s} {a:+.1f}%' for s, a in top_accel)}")

        # Pass memory to brain
        if hasattr(self.brain, 'memory'):
            self.brain.memory = self.memory

    def _brain_tick_loop(self):
        """Main brain loop — runs every tick_interval seconds."""
        # Wait for initial data collection
        log("info", f"Warming up for 10s to collect initial data...")
        self._stop.wait(timeout=10)

        while not self._stop.is_set():
            try:
                self._sync_brain_state()

                # Run correlation break scanner (our best strategy — 197% CAGR)
                now_ms = time.time() * 1000
                corr_signals = self.corr_scanner.scan(_CORR_SYMBOLS, now_ms)
                for cs in corr_signals:
                    from src.engine.signal_detector import SignalPacket
                    self.brain.add_signal(SignalPacket(
                        signal_id=f"corr-{int(now_ms)}-{cs['symbol']}",
                        symbol=cs["symbol"], signal_type="correlation_break",
                        priority=2, timestamp=now_ms,
                        price_usd=self.streams.snapshot.prices.get(cs["symbol"], 0),
                        fear_greed_index=self.streams.snapshot.fear_greed_index,
                        source="correlation_scanner",
                        reasoning=cs["reasoning"],
                        suggested_side=cs["side"],
                        suggested_stop_pct=0.03, suggested_target_pct=0.05,
                        data=cs,
                    ))

                # Claude brain tick — one Haiku call with full state
                decisions = self.brain.tick()
                self.ticks_run += 1

                # Execute decisions
                for decision in decisions:
                    if decision.action in ("BUY", "SELL"):
                        # Get live price for the symbol
                        prices = fetch_binance_prices([decision.symbol], snapshot=self.streams.snapshot)
                        price = prices.get(decision.symbol, 0)
                        if price > 0:
                            decision.entry_price = price
                            pos = self.executor.open_position(decision)
                            if pos:
                                self.trades_opened += 1
                                log("trade", f"OPENED {pos.side} {pos.symbol} ${pos.size_usd:.0f} @ ${pos.entry_price:.4f}")

                    elif decision.action == "CLOSE":
                        # Find and close the position
                        for pos in self.executor.positions:
                            if pos.symbol == decision.symbol:
                                self.executor._close_position(pos, pos.current_price, "claude_decision")
                                # Bug class: prior code recorded pnl from
                                # pos.current_price (last observed mark) rather
                                # than the actual fill price. After the close
                                # lands, the ClosedTrade carries the real
                                # exit_price + post-fee pnl_usd — use those
                                # for memory so the brain learns from truth,
                                # not from the mark it saw a tick ago.
                                last_closed = self.executor.closed_trades[-1] if self.executor.closed_trades else None
                                if last_closed is None or last_closed.position.id != pos.id:
                                    # Close failed (live exchange rejected) —
                                    # position remains open; skip recording.
                                    break
                                pnl_pct = last_closed.pnl_pct * 100
                                pnl_usd = last_closed.pnl_usd
                                self.trades_closed += 1
                                self.memory.record_trade(
                                    symbol=pos.symbol,
                                    pnl_pct=pnl_pct,
                                    pnl_usd=pnl_usd,
                                    exit_reason="claude_decision",
                                    strategy=pos.signal_type,
                                    duration_h=pos.hold_hours,
                                )
                                self.memory.save()
                                break

            except Exception as e:
                log("error", f"Brain tick error: {e} — restarting in 10s")
                self._stop.wait(timeout=10)
                continue

            self._stop.wait(timeout=self.tick_interval)

    def _price_update_loop(self):
        """Poll Binance prices, check stops/targets, feed correlation scanner."""
        while not self._stop.is_set():
            try:
                # Feed prices into acceleration tracker (copy under lock)
                all_prices = self.streams.get_prices_snapshot()
                for symbol, price in all_prices.items():
                    self.accel_tracker.update(symbol, price)

                # Feed correlation scanner with all tracked symbol prices
                now_ms = time.time() * 1000
                for sym in _CORR_SYMBOLS:
                    price = all_prices.get(sym, 0)
                    if price > 0:
                        self.corr_scanner.update_price(sym, price, now_ms)

                # Update position prices and check stops/targets
                symbols = [p.symbol for p in self.executor.positions]
                if symbols:
                    prices = fetch_binance_prices(symbols, snapshot=self.streams.snapshot)
                    for sym, price in prices.items():
                        old_count = len(self.executor.positions)
                        self.executor.update_price(sym, price)

                        # If a position was closed by stop/target, review it
                        if len(self.executor.positions) < old_count:
                            self.trades_closed += 1
                            closed = [t for t in self.executor.closed_trades
                                      if t.position.symbol == sym]
                            if closed:
                                latest = closed[-1]
                                self.brain.review_trade({
                                    "trade_id": latest.position.id,
                                    "symbol": sym,
                                    "side": latest.position.side,
                                    "entry": latest.position.entry_price,
                                    "exit": latest.exit_price,
                                    "pnl_pct": latest.pnl_pct * 100,
                                    "duration_hours": latest.position.hold_hours,
                                    "signal_type": latest.position.signal_type,
                                    "reasoning": latest.position.reasoning,
                                })
                                self.memory.record_trade(
                                    symbol=sym,
                                    pnl_pct=latest.pnl_pct * 100,
                                    pnl_usd=latest.pnl_usd if hasattr(latest, 'pnl_usd') else 0,
                                    exit_reason=latest.exit_reason if hasattr(latest, 'exit_reason') else "stop_target",
                                    strategy=latest.position.signal_type,
                                    duration_h=latest.position.hold_hours,
                                )
                                self.memory.save()

                # Check thesis for each open position
                for pos in list(self.executor.positions):
                    if not getattr(pos, 'thesis_conditions', None) or pos.hold_hours < 0.5:
                        continue  # skip first 30 min
                    tc = pos.thesis_conditions
                    broken = False
                    reason = ""

                    if tc.get("funding_negative"):
                        current_funding = self.streams.snapshot.funding_rates.get(pos.symbol, 0)
                        if current_funding > 0.0005:  # funding flipped meaningfully positive
                            broken = True
                            reason = f"funding flipped positive ({current_funding*100:+.3f}%)"

                    if broken:
                        log("trade", f"THESIS BREAK: {pos.symbol} - {reason}")
                        self.executor._close_position(pos, pos.current_price, "thesis_break")
                        if self.memory:
                            self.memory.record_trade(pos.symbol, pos.unrealized_pnl_pct * 100, pos.unrealized_pnl_usd if hasattr(pos, 'unrealized_pnl_usd') else 0, "thesis_break", pos.signal_type, pos.hold_hours)
                            self.memory.save()
            except Exception as e:
                log("warn", f"Price update error: {e}")

            self._stop.wait(timeout=5)  # check prices every 5s

    def _opus_analysis_loop(self):
        """Hourly deep market analysis using Opus (~$0.015/call)."""
        self._stop.wait(timeout=300)  # wait 5 min for data warmup
        while not self._stop.is_set():
            try:
                if hasattr(self.brain, 'deep_analysis'):
                    analysis = self.brain.deep_analysis()
                    if analysis and self.memory:
                        self.memory.save()
            except Exception as e:
                log("warn", f"Opus analysis failed: {e}")
            self._stop.wait(timeout=3600)  # every hour

    def _stats_loop(self):
        """Print stats periodically."""
        reconcile_counter = 0
        while not self._stop.is_set():
            # Reconcile balance with exchange every 5 heartbeats (~5 min) so
            # funding fees, mark-PnL, and any other untracked debits/credits
            # don't let the displayed balance drift from reality.
            if not self.paper and reconcile_counter % 5 == 0:
                self.executor._reconcile_balance(reason="heartbeat")
                # Refresh funding-fee accounting same cadence (5 min). The
                # method internally rate-limits to 5 min so calling more
                # often is safe but redundant.
                self.executor.refresh_funding_paid_24h()
            reconcile_counter += 1

            stats = self.executor.get_stats()
            api_cost = self.brain.get_daily_cost_estimate()

            positions_str = " | ".join(
                f"{p.symbol} {p.side} {p.unrealized_pnl_pct*100:+.1f}%"
                for p in self.executor.positions
            ) or "none"

            mode = "PAPER" if self.paper else "LIVE"
            funding = self.executor.funding_paid_24h
            funding_str = f" | Fund24h:${funding:+.2f}" if funding is not None else ""
            log("info",
                f"[{mode}] Bal:${stats['balance']:,.0f} | "
                f"Pos:{stats['open_positions']} [{positions_str}] | "
                f"Trades:{stats['total_trades']} ({stats['win_rate']:.0f}%WR) | "
                f"PnL:${stats['total_pnl']:+,.2f}{funding_str} | "
                f"Ticks:{self.ticks_run} Sigs:{self.signals_received} | "
                f"API:${api_cost:.3f}/day")

            # Liveness file: external watchdog reads this and restarts the
            # service if the timestamp is stale (>3 min). Critical because
            # `systemctl is-active` returns "active" for a hung process —
            # we lost ~17h of trading yesterday to a silent hang where the
            # bot was systemd-active but had stopped logging entirely.
            try:
                liveness_path = _PORTFOLIO_FILE.parent / ".heartbeat"
                liveness_path.write_text(str(int(time.time())))
            except Exception as e:
                log("warn", f"liveness write failed: {e}")

            self._stop.wait(timeout=60)

    def start(self):
        """Start the trading engine."""
        mode = "PAPER" if self.paper else "LIVE BINANCE"
        log("info", f"{'='*60}")
        log("info", f"  KAIZEN TRADER — Real-Time Claude Engine [{mode}]")
        log("info", f"  Balance: ${self.executor.balance:,.2f}")
        log("info", f"  Brain tick: every {self.tick_interval}s")
        log("info", f"  Data: Binance + CoinGecko + DexScreener + FGI + Coinbase")
        log("info", f"  Est. daily cost: ~${1440/self.tick_interval * 0.00025:.2f}")
        log("info", f"{'='*60}")

        # Start data streams
        self.streams.start()

        # Start liquidation tracker (Binance !forceOrder@arr) — feeds the
        # liquidation_cascade entry filter and is queryable by the brain via
        # ctx['liquidations']. Cheap to run; ~30 events/sec normal load.
        if not self.paper:
            from src.engine.liquidation_tracker import get_tracker as _liq_tracker
            _liq_tracker().start()
            log("info", "Liquidation tracker started")

            # Start CVD tracker — per-symbol cumulative volume delta from
            # aggTrade. Subscribed dynamically when positions open; provides
            # divergence signal for entry filter. Pre-subscribe to current
            # open positions so velocity has data after restart.
            from src.engine.cvd_tracker import get_tracker as _cvd_tracker
            cvd = _cvd_tracker()
            cvd.start()
            for pos in self.executor.positions:
                cvd.subscribe(pos.symbol)
            log("info", f"CVD tracker started ({len(self.executor.positions)} pre-subs)")

        # Start OKX WS (live mode + okx exchange only)
        if self.okx_public_ws:
            self.okx_public_ws.connect()
            log("info", "OKX public WS started (dynamic ticker subs for open positions)")
        if self.okx_private_ws:
            self.okx_private_ws.connect()
            log("info", "OKX private WS started (real-time orders/positions/balance)")

        # Start processing threads
        threads = [
            ("brain-tick", self._brain_tick_loop),
            ("price-updater", self._price_update_loop),
            ("stats", self._stats_loop),
        ]
        if os.environ.get("ANTHROPIC_API_KEY"):
            threads.append(("opus-analysis", self._opus_analysis_loop))
        for name, target in threads:
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()

        log("info", "Engine running. Brain active.")

    def stop(self):
        """Gracefully stop the engine."""
        log("info", "Stopping engine...")
        self._stop.set()
        self.streams.stop()
        if self.okx_public_ws:
            self.okx_public_ws.disconnect()
        if self.okx_private_ws:
            self.okx_private_ws.disconnect()
        if self.memory:
            self.memory.save()

        stats = self.executor.get_stats()
        log("info", f"Session stats: {stats['total_trades']} trades, "
            f"${stats['total_pnl']:+,.2f} P&L, {stats['win_rate']:.0f}% WR, "
            f"API cost: ${self.brain.get_daily_cost_estimate():.3f}")

    def run_forever(self):
        """Start engine and block until interrupted."""
        self.start()

        def _sig_handler(sig, frame):
            self.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        while not self._stop.is_set():
            self._stop.wait(timeout=1)


def main():
    parser = argparse.ArgumentParser(description="Kaizen Trader — Real-time Claude-powered engine")
    parser.add_argument("--live", action="store_true", help="Enable live Binance execution")
    parser.add_argument("--balance", type=float, default=10_000, help="Starting balance")
    parser.add_argument("--tick", type=int, default=60, help="Brain tick interval in seconds")
    parser.add_argument("--auto-balance", action="store_true", help="Fetch balance from Binance account instead of using --balance")
    parser.add_argument("--confirm", action="store_true", help="Skip live-mode countdown (same as KAIZEN_LIVE_CONFIRMED=true)")
    args = parser.parse_args()

    if args.auto_balance:
        fetched = _fetch_binance_account_balance()
        if fetched is not None:
            args.balance = fetched
            print(f"  Binance account balance: ${args.balance:,.2f}")
        else:
            print("  WARNING: Could not fetch Binance balance, using default ${:.2f}".format(args.balance))

    if args.live:
        confirmed = args.confirm or os.environ.get("KAIZEN_LIVE_CONFIRMED", "").lower() in ("true", "1")
        if confirmed:
            print("\n  *** LIVE TRADING MODE — confirmed via env/flag, starting immediately ***\n")
        else:
            print("\n  *** LIVE TRADING MODE — real money at risk! ***")
            for i in range(3, 0, -1):
                print(f"  Starting in {i}s...", flush=True)
                time.sleep(1)
            print()

    # Initialize database (Convex) if URL is configured
    from src.config import env
    if env.convex_url:
        from src.storage import database
        database.init(env.convex_url)

    # Paper vs live: --live flag wins; otherwise PAPER_TRADING env var; default paper.
    # PAPER_TRADING=false in .env gives live without needing the CLI flag, so the
    # systemd unit can be identical across modes.
    if args.live:
        paper_mode = False
    else:
        paper_env = os.environ.get("PAPER_TRADING", "true").lower()
        paper_mode = paper_env not in ("false", "0", "no", "off")
    # If --auto-balance pulled fresh from the exchange, mark the balance as
    # trusted so the executor's state-restore doesn't clobber it with a stale
    # value from disk. Without this, mid-session deposits go unnoticed.
    engine = TradingEngine(paper=paper_mode, balance=args.balance, tick_interval=args.tick,
                           trust_initial_balance=bool(args.auto_balance))
    try:
        engine.run_forever()
    finally:
        if env.convex_url:
            from src.storage import database
            database.close()


if __name__ == "__main__":
    main()
