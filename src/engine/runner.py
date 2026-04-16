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

from src.engine.data_streams import DataStreams, TokenSignal, fetch_binance_prices, fetch_binance_top_movers, fetch_reddit_crypto_sentiment, fetch_crypto_news
from src.engine.signal_detector import SignalDetector, SignalPacket
from src.engine.claude_brain import ClaudeBrain
from src.engine.executor import Executor
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
                return float(asset.get("balance", 0))
        return None
    except Exception as e:
        log("error", f"Failed to fetch Binance balance: {e}")
        return None


def _fetch_single_kline(sym: str) -> tuple[str, float | None]:
    """Fetch 1h price change % for a single symbol. Returns (symbol, pct_change_or_None)."""
    import json as _json
    try:
        pair = sym.upper() + "USDT"
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={pair}&interval=1h&limit=2"
        req = Request(url, headers={"User-Agent": "kaizen-trader/1.0"})
        with urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read().decode())
        if data and len(data) >= 2:
            prev_close = float(data[-2][4])
            curr_close = float(data[-1][4])
            if prev_close > 0:
                return sym, ((curr_close - prev_close) / prev_close) * 100
    except Exception:
        pass
    return sym, None


def _fetch_1h_kline_changes(symbols: list[str]) -> dict[str, float]:
    """Fetch 1h price change % for a list of symbols from Binance klines API.

    Returns {symbol: pct_change} e.g. {"SOL": 3.5, "DOGE": -1.2}.
    Free, no auth required. Uses thread pool with 10s total timeout.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    changes: dict[str, float] = {}
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_single_kline, sym): sym for sym in symbols}
            for future in as_completed(futures, timeout=10):
                try:
                    sym, change = future.result(timeout=2)
                    if change is not None:
                        changes[sym] = change
                except Exception:
                    continue
    except Exception:
        # Timeout or other error — return whatever data we have
        pass
    return changes


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

    def __init__(self, paper: bool = True, balance: float = 10_000, tick_interval: int = 60):
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

        self.executor = Executor(paper=paper, initial_balance=balance)
        self.streams = DataStreams(on_signal=self._on_raw_signal)
        self.corr_scanner = CorrelationScanner()

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

    def _sync_brain_state(self):
        """Sync executor state into brain so Claude sees current portfolio."""
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

        # Sync social and news signals (FREE data sources)
        try:
            reddit_posts = fetch_reddit_crypto_sentiment()
            bullish = sum(1 for p in reddit_posts if any(kw in p['title'].lower() for kw in ['bull', 'pump', 'rally', 'gain', 'moon', 'surge']))
            bearish = sum(1 for p in reddit_posts if any(kw in p['title'].lower() for kw in ['bear', 'crash', 'dump', 'loss', 'tank']))
            sentiment = (bullish - bearish) / max(1, bullish + bearish) if (bullish + bearish) > 0 else 0
            self.brain.reddit_sentiment = sentiment
            self.brain.reddit_post_count = len(reddit_posts)
            if reddit_posts:
                log("info", f"📱 Reddit sentiment: {sentiment:+.2f} ({bullish} bullish, {bearish} bearish)")
        except Exception as e:
            log("warn", f"Reddit fetch failed: {e}")
            self.brain.reddit_sentiment = 0
            self.brain.reddit_post_count = 0

        try:
            news_items = fetch_crypto_news()
            self.brain.latest_news = news_items[:5] if news_items else []
            if news_items:
                log("info", f"📰 Latest news: {news_items[0]['title'][:60]}")
        except Exception as e:
            log("warn", f"News fetch failed: {e}")
            self.brain.latest_news = []

        # Fetch 1h kline data for top movers — critical for RuleBrain acceleration scoring
        try:
            gainers, losers = fetch_binance_top_movers(limit=20)
            movers = gainers + losers
            symbols_to_check = [m["symbol"] for m in movers][:20]
            hourly_changes = _fetch_1h_kline_changes(symbols_to_check)

            # Inject 1h acceleration into pending signals so RuleBrain can score them
            for sig in self.brain.pending_signals:
                sym = sig.symbol
                if sym in hourly_changes and (sig.data is not None):
                    sig.data["acceleration_1h"] = hourly_changes[sym]
                elif sym in hourly_changes:
                    sig.data = {"acceleration_1h": hourly_changes[sym]}

            if hourly_changes:
                top_accel = max(hourly_changes.items(), key=lambda x: abs(x[1]))
                log("info", f"📊 1h klines: {len(hourly_changes)} symbols, top={top_accel[0]} {top_accel[1]:+.1f}%")
        except Exception as e:
            log("warn", f"1h kline fetch failed: {e}")

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
                        prices = fetch_binance_prices([decision.symbol])
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
                                self.trades_closed += 1
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
                # Feed correlation scanner with all tracked symbol prices
                all_prices = self.streams.snapshot.prices
                now_ms = time.time() * 1000
                for sym in _CORR_SYMBOLS:
                    price = all_prices.get(sym, 0)
                    if price > 0:
                        self.corr_scanner.update_price(sym, price, now_ms)

                # Update position prices and check stops/targets
                symbols = [p.symbol for p in self.executor.positions]
                if symbols:
                    prices = fetch_binance_prices(symbols)
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
            except Exception as e:
                log("warn", f"Price update error: {e}")

            self._stop.wait(timeout=5)  # check prices every 5s

    def _stats_loop(self):
        """Print stats periodically."""
        while not self._stop.is_set():
            stats = self.executor.get_stats()
            api_cost = self.brain.get_daily_cost_estimate()

            positions_str = " | ".join(
                f"{p.symbol} {p.side} {p.unrealized_pnl_pct*100:+.1f}%"
                for p in self.executor.positions
            ) or "none"

            mode = "PAPER" if self.paper else "LIVE"
            log("info",
                f"[{mode}] Bal:${stats['balance']:,.0f} | "
                f"Pos:{stats['open_positions']} [{positions_str}] | "
                f"Trades:{stats['total_trades']} ({stats['win_rate']:.0f}%WR) | "
                f"PnL:${stats['total_pnl']:+,.2f} | "
                f"Ticks:{self.ticks_run} Sigs:{self.signals_received} | "
                f"API:${api_cost:.3f}/day")

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

        # Start processing threads
        threads = [
            threading.Thread(target=self._brain_tick_loop, daemon=True, name="brain-tick"),
            threading.Thread(target=self._price_update_loop, daemon=True, name="price-updater"),
            threading.Thread(target=self._stats_loop, daemon=True, name="stats"),
        ]
        for t in threads:
            t.start()

        log("info", "Engine running. Brain active.")

    def stop(self):
        """Gracefully stop the engine."""
        log("info", "Stopping engine...")
        self._stop.set()
        self.streams.stop()

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

    engine = TradingEngine(paper=not args.live, balance=args.balance, tick_interval=args.tick)
    try:
        engine.run_forever()
    finally:
        if env.convex_url:
            from src.storage import database
            database.close()


if __name__ == "__main__":
    main()
