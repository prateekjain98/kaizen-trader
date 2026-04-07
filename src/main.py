"""Self-Healing AI Crypto Trader — Main Entry Point."""

import signal
import sys
import threading
import time
from dataclasses import asdict

from src.config import env, default_scanner_config
from src.storage.database import log
from src.self_healing.log_analyzer import run_analysis
from src.self_healing.healer import on_position_closed
from src.types import ScannerConfig

# Mutable config — self-healer patches this live
config = ScannerConfig(**asdict(default_scanner_config))


def _analysis_loop() -> None:
    """Periodically run Claude log analysis."""
    while True:
        time.sleep(env.log_analysis_interval_mins * 60)
        try:
            run_analysis(config)
        except Exception as err:
            log("error", f"Log analysis failed: {err}")


def main() -> None:
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

    # Claude log analysis loop
    if env.anthropic_api_key:
        analysis_thread = threading.Thread(target=_analysis_loop, daemon=True)
        analysis_thread.start()
        log("info", f"Claude log analysis scheduled every {env.log_analysis_interval_mins} minutes")

    log("info", f"""
----------------------------------------------
  Strategies:
    momentum_swing        * momentum_scalp
    listing_pump          * whale_accumulation
    mean_reversion        * funding_extreme
    liquidation_cascade   * orderbook_imbalance
    narrative_momentum    * correlation_break
    protocol_revenue      * fear_greed_contrarian

  Self-healing:
    immediate  — loss diagnosis + parameter patch after each trade
    periodic   — Claude log analysis every {env.log_analysis_interval_mins}m
----------------------------------------------""")

    # Graceful shutdown
    if threading.current_thread() is threading.main_thread():
        def handle_sigint(sig, frame):
            log("info", "Shutting down gracefully...")
            sys.exit(0)
        signal.signal(signal.SIGINT, handle_sigint)

    # Block forever
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("info", "Shutting down gracefully...")


if __name__ == "__main__":
    main()
