"""Engine-local logging — prints to stdout, optionally forwards to Convex if available."""

import datetime
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
_logger = logging.getLogger("kaizen-engine")


def log(level: str, message: str, **kwargs):
    """Log a message. Always prints to stdout. Optionally stores in Convex."""
    symbol = kwargs.get("symbol", "")
    prefix = f"[{symbol}] " if symbol else ""

    if level == "error":
        _logger.error(f"{prefix}{message}")
    elif level == "warn":
        _logger.warning(f"{prefix}{message}")
    elif level == "trade":
        _logger.info(f"💰 {prefix}{message}")
    else:
        _logger.info(f"{prefix}{message}")

    # Try forwarding to Convex if available (non-blocking)
    try:
        from src.storage.database import log as convex_log
        convex_log(level, message, **kwargs)
    except Exception:
        pass  # Convex not available — that's fine
