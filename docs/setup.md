# Setup Guide

## Prerequisites

- Python 3.11+
- SQLite (included with Python)
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

# ── Optional — dual-write to Convex for real-time dashboard ───────────────────
CONVEX_URL=

# ── Optional — auto GitHub issue creation ─────────────────────────────────────
GITHUB_REPO=                      # e.g., prateekjain98/kaizen-trader

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH=trader.db

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

```bash
# Win rate by strategy
sqlite3 trader.db "
  SELECT strategy,
    COUNT(*) as trades,
    ROUND(100.0 * SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) as win_pct,
    ROUND(SUM(pnl_usd), 2) as total_pnl
  FROM positions WHERE status='closed'
  GROUP BY strategy ORDER BY total_pnl DESC;"

# Recent self-healer actions
sqlite3 trader.db "
  SELECT symbol, loss_reason, action, datetime(timestamp/1000, 'unixepoch') as at
  FROM diagnoses ORDER BY timestamp DESC LIMIT 20;"

# Check config evolution
sqlite3 trader.db "
  SELECT reason, config, timestamp
  FROM scanner_config_history
  ORDER BY timestamp DESC LIMIT 20;"
```

## Deployment

### Railway (recommended)

```bash
# Procfile already configured:
# bot: python -m src.main

# Push to Railway — auto-deploys from GitHub
railway up
```

Set environment variables in the Railway dashboard. Health check at `/health` on port 8080. Persistent volume at `/data` for SQLite fallback.

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
