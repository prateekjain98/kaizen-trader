# Setup Guide

## Prerequisites

- Python 3.11+
- Binance Futures account (or OKX)
- `pip install -r requirements.txt` (or `pip install dotenv requests anthropic websocket-client`)

## Configuration

```bash
cp .env.example .env
```

### Environment variables

```bash
# ── Required ─────────────────────────────────────────────────────────────────
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
PAPER_TRADING=false

# ── Optional -- switches to OKX ──────────────────────────────────────────────
EXCHANGE=okx
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=

# ── Optional -- enables Claude brain ($0.50/day) ─────────────────────────────
ANTHROPIC_API_KEY=

# ── Optional -- Convex database ──────────────────────────────────────────────
CONVEX_URL=

# ── Risk limits ──────────────────────────────────────────────────────────────
MAX_POSITION_USD=20
MAX_DAILY_LOSS_USD=5
MAX_OPEN_POSITIONS=4
```

## Running

```bash
# Live trading
python -m src.engine.runner --live --auto-balance --tick 60

# Paper trading
python -m src.engine.runner --tick 60

# Watchdog (stop-loss safety net)
python watchdog.py
```

## Deployment

### Railway (recommended)

Set environment variables in the Railway dashboard.

Procfile:
```
python -m src.engine.runner --live --auto-balance --tick 60 --confirm
```

### Docker

Standard Python image, install requirements, same command as above.

### Notes

- The engine auto-restarts brain tick on crash via watchdog thread
- Binance API keys can be IP-restricted. If your ISP rotates IPs, remove the IP restriction or use a VPS with a static IP
- OKX does not have this issue

## Legacy entry point

```bash
# Self-healing loop (backtesting, parameter tuning)
python -m src.main
```

## Scripts

```bash
# Trigger Claude log analysis manually
python scripts/analyze_logs.py

# Performance report
python scripts/performance.py
python scripts/performance.py --last 50
python scripts/performance.py --csv > trades.csv
```
