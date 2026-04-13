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
import signal
import sys
import threading
import time
from collections import deque

from src.engine.data_streams import DataStreams, TokenSignal, fetch_binance_prices
from src.engine.signal_detector import SignalDetector, SignalPacket
from src.engine.claude_brain import ClaudeBrain
from src.engine.executor import Executor
from src.engine.correlation_scanner import CorrelationScanner
from src.engine.log import log

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
        self.brain = ClaudeBrain(balance=balance)
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
                log("error", f"Brain tick error: {e}")

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

        log("info", "Engine running. Claude brain active.")

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
    args = parser.parse_args()

    if args.live:
        print("\n  *** LIVE TRADING MODE — real money at risk! ***\n")
        confirm = input("  Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("  Aborted.")
            return

    engine = TradingEngine(paper=not args.live, balance=args.balance, tick_interval=args.tick)
    engine.run_forever()


if __name__ == "__main__":
    main()
