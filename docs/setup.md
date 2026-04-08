# Setup Guide

## Prerequisites

- Python 3.11+
- Convex account (free tier — database backend)
- Coinbase Advanced Trade account (for price feed + execution)
- Anthropic API key (for self-healing log analysis)

## Installation

```bash
git clone https://github.com/prateekjain98/kaizen-trader
cd kaizen-trader

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Configuration

```bash
cp .env.example .env
```

Edit `.env` with your keys. Paper trading is enabled by default.

### Environment variables

```bash
# ── Mode ──────────────────────────────────────────────────────────────────────
PAPER_TRADING=true                # always start here

# ── Required ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY=                # Claude log analysis
COINBASE_API_KEY=                 # price feed + execution
COINBASE_API_SECRET=

# ── Recommended ───────────────────────────────────────────────────────────────
LUNARCRUSH_API_KEY=               # social signals (Twitter/Reddit/YouTube/TikTok)
CRYPTOPANIC_TOKEN=                # news sentiment

# ── Optional — enables additional strategies ──────────────────────────────────
BINANCE_API_KEY=                  # funding_extreme, liquidation_cascade
BINANCE_API_SECRET=
WHALE_ALERT_API_KEY=              # whale_accumulation

# ── Risk limits ───────────────────────────────────────────────────────────────
MAX_POSITION_USD=100
MAX_DAILY_LOSS_USD=300
MAX_OPEN_POSITIONS=5

# ── Self-healing schedule ─────────────────────────────────────────────────────
LOG_ANALYSIS_INTERVAL_MINS=60
MIN_TRADES_FOR_ANALYSIS=10

# ── Required — Convex database ────────────────────────────────────────────────
CONVEX_URL=                       # e.g., https://your-project.convex.cloud

# ── Optional — auto GitHub issue creation ─────────────────────────────────────
GITHUB_REPO=                      # e.g., prateekjain98/kaizen-trader

# ── Health check ──────────────────────────────────────────────────────────────
PORT=8080
```

Strategies degrade gracefully when their data source isn't configured — the system runs on whatever signals are available.

## Running

```bash
python -m src.main
```

The bot starts all threads (WebSocket feed, signal fetchers, exit checker, self-healing, strategy evaluation) and exposes a health endpoint at `http://localhost:8080/health`.

## Running tests

```bash
python -m pytest tests/ -v
```

## Scripts

```bash
# Trigger Claude log analysis manually
python scripts/analyze_logs.py

# Performance report
python scripts/performance.py
python scripts/performance.py --last 50
python scripts/performance.py --csv > trades.csv

# Backtest a strategy
python scripts/backtest.py --symbol BTC --start 2025-01-01 --end 2025-06-01
python scripts/backtest.py --symbol ETH --start 2025-03-01 --end 2025-06-01 --commission 0.001
```

## Querying the database

All data is stored in Convex. Use the Convex dashboard at your `CONVEX_URL` to browse tables directly, or use the bot's Python read APIs:

```python
from src.storage.database import (
    get_open_positions, get_closed_trades, get_recent_logs,
    get_recent_diagnoses, get_trade_journal,
)

# Recent closed trades
closed = get_closed_trades(limit=100)

# Self-healer diagnoses
diagnoses = get_recent_diagnoses(limit=20)

# Error/warning logs
logs = get_recent_logs(limit=50, level="error")
```

## Deployment

### Railway (recommended)

```bash
# Procfile already configured:
# bot: python -m src.main

# Push to Railway — auto-deploys from GitHub
railway up
```

Set environment variables in the Railway dashboard. Health check at `/health` on port 8080.

### Docker (alternative)

```bash
docker build -t kaizen-trader .
docker run --env-file .env kaizen-trader
```

## Adding a strategy

Every strategy is a function returning `Optional[TradeSignal]`:

```python
# src/strategies/my_strategy.py
import uuid, time
from typing import Optional
from src.types import TradeSignal, ScannerConfig, MarketContext

def scan_my_strategy(
    symbol: str,
    product_id: str,
    current_price: float,
    config: ScannerConfig,
    ctx: MarketContext,
) -> Optional[TradeSignal]:
    if ctx.fear_greed_index > 30:
        return None

    return TradeSignal(
        id=str(uuid.uuid4()),
        symbol=symbol,
        product_id=product_id,
        strategy="my_strategy",
        side="long",
        tier="swing",
        score=72,
        confidence="medium",
        sources=["fear_greed"],
        reasoning=f"{symbol} in extreme fear - contrarian entry",
        entry_price=current_price,
        stop_price=current_price * 0.95,
        suggested_size_usd=100,
        expires_at=int(time.time() * 1000) + 3_600_000,
        created_at=int(time.time() * 1000),
    )
```

The strategy registry auto-discovers any `scan_*` or `on_*` function in `src/strategies/`. No manual registration needed — drop a file and restart.

For richer metadata, define a `STRATEGY_META` dict in the module:

```python
STRATEGY_META = {
    "strategies": [
        {"id": "my_strategy", "function": "scan_my_strategy", "tier": "swing", "description": "..."}
    ],
    "signal_sources": ["fear_greed"],
}
```
