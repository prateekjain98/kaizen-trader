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

    # Try forwarding to Convex if available (non-blocking).
    # Surface failures to stderr so Convex outages don't go unnoticed; only
    # the very first failure is logged loudly to avoid log spam if Convex
    # is down for an extended period.
    try:
        from src.storage.database import log as convex_log
        convex_log(level, message, **kwargs)
    except Exception as e:
        if not getattr(log, "_convex_warned", False):
            _logger.error(f"Convex forward FAILED (suppressing further): {type(e).__name__}: {e}")
            log._convex_warned = True
