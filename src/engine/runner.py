"""Real-time trading engine runner.

Wires together: DataStreams → SignalDetector → ClaudeBrain → Executor

Usage:
    python -m src.engine.runner              # paper trading
    python -m src.engine.runner --live       # live Binance execution
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
from src.engine.log import log


class TradingEngine:
    """Real-time Claude-powered trading engine.

    Pipeline:
        DataStreams emit TokenSignals
        → SignalDetector converts to SignalPackets
        → ClaudeBrain pre-filters (Haiku) then analyzes (Sonnet)
        → Executor places orders on Binance

    Cost: ~$0.50-2.00/day in Claude API calls.
    """

    def __init__(self, paper: bool = True, balance: float = 10_000):
        self.paper = paper
        self.signal_queue: deque[SignalPacket] = deque(maxlen=100)
        self._stop = threading.Event()

        # Components
        self.detector = SignalDetector()
        self.brain = ClaudeBrain(balance=balance)
        self.executor = Executor(paper=paper, initial_balance=balance)
        self.streams = DataStreams(on_signal=self._on_raw_signal)

        # Stats
        self.signals_received = 0
        self.signals_filtered = 0
        self.signals_analyzed = 0
        self.trades_opened = 0

    def _on_raw_signal(self, signal: TokenSignal):
        """Called by DataStreams when a new signal arrives."""
        self.signals_received += 1

        # Convert to SignalPacket
        packet = self.detector.process(signal, self.streams.snapshot)
        if not packet:
            return

        self.signal_queue.append(packet)

    def _process_signal_queue(self):
        """Process queued signals through Claude brain → executor."""
        while self.signal_queue and not self._stop.is_set():
            packet = self.signal_queue.popleft()

            # Step 1: Haiku pre-filter
            if not self.brain.pre_filter(packet):
                self.signals_filtered += 1
                continue

            self.signals_analyzed += 1

            # Step 2: Sonnet full analysis
            decision = self.brain.analyze(packet)
            if not decision:
                continue

            # Step 3: Execute
            if decision.action == "BUY" and not self.executor.has_position(decision.symbol):
                pos = self.executor.open_position(decision)
                if pos:
                    self.trades_opened += 1
                    # Update brain's position tracking
                    self.brain.open_positions.append({
                        "symbol": pos.symbol, "side": pos.side,
                        "size_usd": pos.size_usd, "entry": pos.entry_price,
                    })

    def _price_update_loop(self):
        """Poll Binance prices for open positions and check stops/targets."""
        while not self._stop.is_set():
            try:
                symbols = [p.symbol for p in self.executor.positions]
                if symbols:
                    prices = fetch_binance_prices(symbols)
                    for sym, price in prices.items():
                        old_count = len(self.executor.positions)
                        self.executor.update_price(sym, price)

                        # If a position was closed, review it with Claude
                        if len(self.executor.positions) < old_count:
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
                                # Sync brain state
                                self.brain.open_positions = [
                                    {"symbol": p.symbol, "side": p.side,
                                     "size_usd": p.size_usd, "entry": p.entry_price}
                                    for p in self.executor.positions
                                ]
                                self.brain.balance = self.executor.balance
                                self.brain.daily_pnl = self.executor.daily_pnl
            except Exception as e:
                log("warn", f"Price update error: {e}")

            self._stop.wait(timeout=5)  # check prices every 5s

    def _stats_loop(self):
        """Print stats periodically."""
        while not self._stop.is_set():
            stats = self.executor.get_stats()
            api_cost = self.brain.get_daily_cost_estimate()
            stats["daily_api_cost"] = api_cost

            positions_str = " | ".join(
                f"{p.symbol} {p.side} {p.unrealized_pnl_pct*100:+.1f}%"
                for p in self.executor.positions
            ) or "none"

            mode = "PAPER" if self.paper else "LIVE"
            log("info",
                f"[{mode}] Balance: ${stats['balance']:,.2f} | "
                f"Positions: {stats['open_positions']} [{positions_str}] | "
                f"Trades: {stats['total_trades']} ({stats['win_rate']:.0f}% WR) | "
                f"P&L: ${stats['total_pnl']:+,.2f} | "
                f"Signals: {self.signals_received} recv, {self.signals_analyzed} analyzed, {self.trades_opened} traded | "
                f"API cost: ${api_cost:.3f}")

            self._stop.wait(timeout=60)  # stats every 60s

    def start(self):
        """Start the trading engine."""
        mode = "PAPER" if self.paper else "LIVE BINANCE"
        log("info", f"Starting real-time trading engine [{mode}]")
        log("info", f"Balance: ${self.executor.balance:,.2f}")
        log("info", "Components: DataStreams → SignalDetector → ClaudeBrain → Executor")
        log("info", "Data: Binance + CoinGecko + DexScreener + Alternative.me + Coinbase")
        log("info", "")

        # Start data streams
        self.streams.start()

        # Start processing threads
        threads = [
            threading.Thread(target=self._signal_processor_loop, daemon=True, name="signal-processor"),
            threading.Thread(target=self._price_update_loop, daemon=True, name="price-updater"),
            threading.Thread(target=self._stats_loop, daemon=True, name="stats"),
        ]
        for t in threads:
            t.start()

        log("info", "Engine started. Monitoring markets...")

    def _signal_processor_loop(self):
        """Continuously process signal queue."""
        while not self._stop.is_set():
            try:
                self._process_signal_queue()
            except Exception as e:
                log("error", f"Signal processor error: {e}")
            self._stop.wait(timeout=1)  # check queue every 1s

    def stop(self):
        """Gracefully stop the engine."""
        log("info", "Stopping engine...")
        self._stop.set()
        self.streams.stop()

        stats = self.executor.get_stats()
        log("info", f"Final stats: {stats['total_trades']} trades, "
            f"${stats['total_pnl']:+,.2f} P&L, {stats['win_rate']:.0f}% WR")

    def run_forever(self):
        """Start engine and block until interrupted."""
        self.start()

        # Handle Ctrl+C
        def _sig_handler(sig, frame):
            self.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

        while not self._stop.is_set():
            self._stop.wait(timeout=1)


def main():
    parser = argparse.ArgumentParser(description="Kaizen Trader — Real-time Claude-powered engine")
    parser.add_argument("--live", action="store_true", help="Enable live Binance execution (default: paper)")
    parser.add_argument("--balance", type=float, default=10_000, help="Starting balance (default: $10,000)")
    args = parser.parse_args()

    if args.live:
        print("⚠️  LIVE TRADING MODE — real money at risk!")
        confirm = input("Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            return

    engine = TradingEngine(paper=not args.live, balance=args.balance)
    engine.run_forever()


if __name__ == "__main__":
    main()
