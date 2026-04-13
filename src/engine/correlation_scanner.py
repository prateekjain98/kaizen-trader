"""Real-time correlation break scanner.

Our highest-CAGR strategy (197%) — runs every hour using live WS prices.
Computes BTC-alt linear regression and detects divergences.

No LLM calls — pure math, runs in <10ms.
"""

import time
from typing import Optional
from src.engine.log import log


class CorrelationScanner:
    """Detects BTC-alt correlation breaks in real-time.

    Every hour, computes:
        1. BTC 1h return
        2. Alt 1h return for each tracked symbol
        3. Linear regression: expected_alt = alpha + beta * btc_return
        4. Divergence = actual_alt - expected_alt
        5. Signal if |divergence| > threshold
    """

    # Symbols with consistently low WR — skip
    _BLACKLIST = {"EOS", "QTUM", "TRX", "LTC", "BNT", "NEO", "ATOM", "ADA", "ICX", "THETA"}

    # Thresholds from backtest (57.2% WR, 197% CAGR)
    LONG_THRESHOLD = -0.08   # alt underperformed BTC by 8%+
    SHORT_THRESHOLD = 0.065  # alt overperformed BTC by 6.5%+

    def __init__(self):
        self._price_history: dict[str, list[tuple[float, float]]] = {}  # symbol -> [(timestamp, price)]
        self._corr_history: dict[str, list[tuple[float, float]]] = {}   # symbol -> [(btc_pct, alt_pct)]
        self._last_scan_ms: float = 0
        self._scan_interval_ms = 3_600_000  # 1 hour

    def update_price(self, symbol: str, price: float, timestamp_ms: float):
        """Feed real-time price data from WebSocket."""
        hist = self._price_history.setdefault(symbol, [])
        hist.append((timestamp_ms, price))
        # Keep last 2 hours of ticks (for computing hourly returns)
        cutoff = timestamp_ms - 7_200_000
        self._price_history[symbol] = [(t, p) for t, p in hist if t > cutoff]

    def _get_hourly_return(self, symbol: str, now_ms: float) -> Optional[float]:
        """Compute 1h return from price history."""
        hist = self._price_history.get(symbol, [])
        if len(hist) < 2:
            return None

        current_price = hist[-1][1]
        # Find price ~1 hour ago
        target_ts = now_ms - 3_600_000
        past_price = None
        for ts, price in reversed(hist):
            if ts <= target_ts:
                past_price = price
                break

        if past_price is None or past_price == 0:
            return None

        return (current_price - past_price) / past_price

    def scan(self, symbols: list[str], now_ms: float) -> list[dict]:
        """Run correlation break scan. Call every hour.

        Returns list of signal dicts:
            {"symbol": "SOL", "side": "long", "divergence": -0.09, "score": 75}
        """
        # Rate limit to once per hour
        if now_ms - self._last_scan_ms < self._scan_interval_ms:
            return []
        self._last_scan_ms = now_ms

        btc_return = self._get_hourly_return("BTC", now_ms)
        if btc_return is None:
            return []

        signals = []
        for sym in symbols:
            if sym in self._BLACKLIST or sym == "BTC":
                continue

            alt_return = self._get_hourly_return(sym, now_ms)
            if alt_return is None:
                continue

            # Build correlation history
            hist = self._corr_history.setdefault(sym, [])
            hist.append((btc_return, alt_return))
            if len(hist) > 200:
                self._corr_history[sym] = hist[-200:]

            if len(hist) < 24:
                continue

            # Linear regression: expected_alt = alpha + beta * btc_return
            n = len(hist)
            sum_x = sum(h[0] for h in hist)
            sum_y = sum(h[1] for h in hist)
            sum_xy = sum(h[0] * h[1] for h in hist)
            sum_xx = sum(h[0] ** 2 for h in hist)
            denom = n * sum_xx - sum_x * sum_x
            if abs(denom) < 1e-10:
                continue

            beta = (n * sum_xy - sum_x * sum_y) / denom
            alpha = (sum_y - beta * sum_x) / n
            expected = alpha + beta * btc_return
            divergence = alt_return - expected

            # Signal on significant divergence
            if divergence < self.LONG_THRESHOLD:
                div_score = min(30, abs(divergence) * 400)
                score = min(80, 50 + div_score)
                signals.append({
                    "symbol": sym, "side": "long",
                    "divergence": divergence, "score": score,
                    "reasoning": f"{sym} underperforming BTC by {divergence*100:.1f}% (expected {expected*100:.1f}%, actual {alt_return*100:.1f}%)",
                })

            elif divergence > self.SHORT_THRESHOLD:
                div_score = min(28, divergence * 350)
                score = min(78, 48 + div_score)
                signals.append({
                    "symbol": sym, "side": "short",
                    "divergence": divergence, "score": score,
                    "reasoning": f"{sym} overperforming BTC by {divergence*100:.1f}% (expected {expected*100:.1f}%, actual {alt_return*100:.1f}%)",
                })

        if signals:
            log("info", f"Correlation scanner: {len(signals)} signals from {len(symbols)} symbols")

        return signals
