"""Visual chart analysis using Claude Vision.

Renders candlestick charts as PNG and sends to Claude for visual pattern recognition.
Uses claude-sonnet-4-20250514 for cost efficiency.
"""

import base64
import io
from typing import Optional

import anthropic

from src.config import env
from src.indicators.core import OHLCV, get_candles, get_snapshot
from src.storage.database import log


def render_chart(symbol: str, candles: Optional[list[OHLCV]] = None) -> Optional[bytes]:
    """Render a candlestick chart as PNG bytes.

    Returns None if matplotlib is not available or insufficient data.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # non-interactive backend
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime
    except ImportError:
        return None

    if candles is None:
        candles = get_candles(symbol)
    if len(candles) < 20:
        return None

    # Prepare data
    dates = [datetime.fromtimestamp(c.ts / 1000) for c in candles]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[3, 1],
                                    sharex=True, gridspec_kw={"hspace": 0.05})

    # Candlestick chart
    for i in range(len(candles)):
        color = "#26a69a" if closes[i] >= opens[i] else "#ef5350"
        ax1.plot([dates[i], dates[i]], [lows[i], highs[i]], color=color, linewidth=0.8)
        ax1.plot([dates[i], dates[i]], [opens[i], closes[i]], color=color, linewidth=3)

    # EMA overlays
    snap = get_snapshot(symbol)
    if snap:
        close_vals = closes
        if len(close_vals) >= 20:
            # Simple moving average for visual (not exact EMA, but close enough for chart)
            from src.indicators.core import compute_ema_series
            ema20 = compute_ema_series(close_vals, min(20, len(close_vals)))
            if ema20:
                offset = len(dates) - len(ema20)
                ax1.plot(dates[offset:], ema20, color="#2196F3", linewidth=1, label="EMA 20", alpha=0.8)
            if len(close_vals) >= 50:
                ema50 = compute_ema_series(close_vals, 50)
                if ema50:
                    offset = len(dates) - len(ema50)
                    ax1.plot(dates[offset:], ema50, color="#FF9800", linewidth=1, label="EMA 50", alpha=0.8)

    ax1.set_ylabel("Price")
    ax1.set_title(f"{symbol} — 1m candles")
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.3)

    # Volume bars
    colors = ["#26a69a" if c >= o else "#ef5350" for o, c in zip(opens, closes)]
    ax2.bar(dates, volumes, color=colors, width=0.0005, alpha=0.7)
    ax2.set_ylabel("Volume")
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()

    # Render to bytes
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def analyze_chart(
    symbol: str,
    chart_png: bytes,
    context: str = "",
) -> Optional[str]:
    """Send a chart image to Claude Vision for visual pattern analysis.

    Uses claude-sonnet-4-20250514 for cost efficiency (~10x cheaper than Opus).
    Returns Claude's analysis text, or None on error.
    """
    if not env.anthropic_api_key:
        return None

    b64_image = base64.b64encode(chart_png).decode("utf-8")

    prompt = f"""Analyze this cryptocurrency chart for {symbol}. Identify:
1. Key support and resistance levels
2. Chart patterns (head & shoulders, double top/bottom, flags, wedges, etc.)
3. Trend direction and strength
4. Volume patterns (divergences, climax volumes)
5. Any actionable signals

{f"Additional context: {context}" if context else ""}

Be concise and specific with price levels."""

    client = anthropic.Anthropic(api_key=env.anthropic_api_key)

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64_image,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    },
                ],
            }],
        )
        block = message.content[0]
        if block.type == "text":
            return block.text
        return None
    except Exception as err:
        log("warn", f"Chart analysis failed for {symbol}: {err}", symbol=symbol)
        return None
