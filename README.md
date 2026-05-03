# Kaizen Trader

Autonomous perpetual-futures crypto trading engine that runs 24/7, detects multi-source trading signals, and executes on Binance or OKX with built-in risk management.

**Core loop:** DataStreams → SignalDetector → RuleBrain (zero cost) or ClaudeBrain (~$1-2/day) → Executor. Every 60 seconds, the brain reads all open positions and new signals, makes BUY/CLOSE decisions, and the executor fills orders.

---

## Architecture

```
                    DataStreams (11 free APIs)
                           │
                           ▼
                   ┌─────────────────────┐
                   │ SignalDetector      │
                   │ filter + rank       │
                   └────────┬────────────┘
                            │
                   ┌────────┴────────┐
                   ▼                 ▼
            ┌────────────┐    ┌────────────────┐
            │ RuleBrain  │    │ ClaudeBrain    │
            │ (zero $)   │    │ (Haiku/Sonnet) │
            │ 12-factor  │    │ LLM decisions  │
            │ scoring    │    │ every 60s      │
            └────────┬───┘    └────────┬───────┘
                     │                 │
                     └────────┬────────┘
                              ▼
                    ┌──────────────────────┐
                    │ Executor             │
                    │ - position tracking  │
                    │ - stop/target checks │
                    │ - trailing stops     │
                    │ - 1x leverage only   │
                    └──────────────────────┘
                              │
                    ┌─────────┴──────────┐
                    ▼                    ▼
            ┌────────────────┐   ┌───────────────┐
            │ Binance Futures│   │ OKX Perpetuals│
            │ (live mode)    │   │ (live mode)   │
            └────────────────┘   └───────────────┘

Background: watchdog.py (stop-loss safety net, separate process)
Deployment: systemd units on GCP VM, auto-deploy on push to main via GitHub Actions
```

---

## Quick Start

**Paper trading (no secrets needed):**
```bash
git clone https://github.com/yourusername/kaizen-trader.git
cd kaizen-trader
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.engine.runner --paper
```

**Live trading on Binance (with 3-second countdown):**
```bash
cp .env.example .env
# Edit .env: set BINANCE_API_KEY, BINANCE_API_SECRET
PAPER_TRADING=false python -m src.engine.runner --auto-balance --tick 60
```

**Live trading on OKX:**
```bash
EXCHANGE=okx PAPER_TRADING=false python -m src.engine.runner --auto-balance --tick 60
```

---

## Configuration

**Environment variables:**

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `PAPER_TRADING` | No | `true` | Set to `false` for live execution |
| `EXCHANGE` | No | `binance` | `binance` or `okx` |
| `BINANCE_API_KEY` | Live Binance | — | Binance Futures API key |
| `BINANCE_API_SECRET` | Live Binance | — | Binance Futures API secret |
| `OKX_API_KEY` | Live OKX | — | OKX API key |
| `OKX_API_SECRET` | Live OKX | — | OKX API secret |
| `OKX_PASSPHRASE` | Live OKX | — | OKX passphrase |
| `ANTHROPIC_API_KEY` | Optional | — | Enables ClaudeBrain. Without it, RuleBrain is used at zero cost |
| `LIQUIDATION_CASCADE_ENABLED` | No | `false` | Enable liquidation cascade fade trades |
| `FUNDING_CARRY_ENABLED` | No | `false` | Enable cross-sectional funding carry |
| `CONVEX_URL` | Optional | — | Convex serverless DB for persistent state |

---

## Strategies

Each strategy maps to a signal type. When the brain decides to BUY, it sets stop/target percentages based on the strategy's empirical risk profile.

| Strategy | Signal Source | Entry Trigger | Stop / Target | Thesis |
|----------|---------------|---------------|----------------|--------|
| **Funding Squeeze** | Binance funding | rate < -0.1% + 1h accel | 10% / 25% | Crowded shorts cascade liquidate, explosive upside. You earn funding payments while holding. |
| **Correlation Break** | 4h BTC-alt divergence | alt underperf. BTC >1.5% | 8% / 15% | Blue-chip alts mean-revert to BTC correlation within 2-4 days. |
| **Listing Pump** | Binance/Coinbase listing | new listing <6h old | 10% / 20% | Proven 77% WR on Coinbase listings (backtest: +474% cumulative). Event-driven; time-sensitive. |
| **FGI Contrarian** | Fear & Greed Index | FGI ≤20 (extreme fear) | 8% / 15% | Contrarian BTC/ETH long at panic bottoms. Backtest: 61.4% WR. |
| **Trending Breakout** | CoinGecko trending top-3 | token newly trending | 8% / 15% | Newly trending tokens with positive 24h momentum. |
| **Liquidation Cascade Fade** | Binance !forceOrder | >250k 5min liquidations | 3.5% / 9% | Wicks reverse fast (5-30min). Backtest: 60%+ WR. |
| **Funding Carry (Long)** | Binance funding rank | longs in bottom 5% | 6% / 10% | Cross-sectional: deepen where short pain is highest. |
| **Funding Carry (Short)** | Binance funding rank | shorts in bottom 5% | 6% / 10% | Load where long pain is highest. |
| **Orderbook Imbalance** | Binance L2 depth (OBI-F) | persistent skew opposite 1h trend | 2% / 3% | Fast mean-revert (arXiv 2507.22712). Paper-thin stops. |
| **Mempool Stress** | BTC on-chain fee regime | fees elevated, FGI >70 | 4% / 7% | BTC fee stress + greed = post-rally top fade short. BTC-only. |
| **Stable Flow (Bull)** | DefiLlama stablecoin net | >$100M issuance/24h | 6% / 12% | Risk-on: stablecoins flooding in. BTC/ETH long. |
| **Stable Flow (Bear)** | DefiLlama stablecoin net | >$100M redemption/24h | 5% / 8% | Risk-off: stablecoins fleeing. BTC/ETH short. |
| **Chain TVL Flow (Bull)** | DefiLlama chain TVL | L1/L2 TVL +7d up | 6% / 12% | Capital rotating INTO an ecosystem. Long tokens native to that chain. |
| **Chain TVL Flow (Bear)** | DefiLlama chain TVL | L1/L2 TVL +7d down | 5% / 8% | Capital rotating OUT. Short natives. |

---

## Risk Management

**Hard constraints:**
- `MAX_POSITION_USD` = $20 per position (configurable)
- `MAX_DAILY_LOSS_USD` = $50 per day (halts all trading if breached)
- `MAX_POSITIONS` = 4 concurrent positions
- `MAX_BALANCE_DEPLOYED_PCT` = 0.80 (leave 20% dry powder)
- `MIN_SCORE_TO_TRADE` = 60 (RuleBrain only; ClaudeBrain scores differently)
- **Leverage:** 1x always, no exceptions

**Cooldown rules (anti-revenge-trading):**
- Per-symbol: After 2 consecutive losses on a symbol, 4-hour cooldown before re-entry
- Per-strategy: After 3 consecutive losses on a strategy, 30-minute cooldown before re-entry

**Exit conditions (any triggers a close):**
1. Stop-loss hit (hard stop at `entry_price * (1 - stop_pct)`)
2. Take-profit hit (target at `entry_price * (1 + target_pct)`)
3. Trailing stop activated: once position reaches 1.5× stop distance profit, stop trails upward
4. Hold timeout: max 48 hours
5. Fast-cut: if position has held >30min with velocity_5min ≤ -2% sustained downtrend, close immediately
6. Chop exit (RuleBrain): if held >60min with <2% movement, close (dead capital)
7. Thesis break: if original thesis condition flips (e.g., funding flipped positive on a squeeze), close
8. Daily loss limit: once portfolio hits `MAX_DAILY_LOSS_USD` in losses, all trading halts for that calendar day

**Watchdog (separate safety net):**
- Runs in `/watchdog.py` as a separate systemd service
- Polls open positions every 10 seconds
- Closes any position that hits a 15% stop or 40% target (configurable via `/tmp/watchdog_stops.json`)
- Survives main engine restarts and maintains safety even if the brain hangs

---

## Backtesting

**Run a backtest:**
```bash
python scripts/run_live_backtest.py \
  --symbols BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX \
  --days 90 \
  --min-score 60
```

**All flags:**

| Flag | Description | Default |
|------|-------------|---------|
| `--symbols` | Comma-separated list | `BTC,ETH,SOL,BNB,XRP,DOGE,ADA,AVAX` |
| `--start` | Start date (YYYY-MM-DD UTC) | — |
| `--end` | End date (YYYY-MM-DD UTC) | — |
| `--days` | Lookback window from today | — |
| `--balance` | Initial balance in USDT | `1000.0` |
| `--out` | Output JSON path | `data/backtest_<timestamp>.json` |
| `--no-filters` | Disable entry filter chain (brain-only scoring) | OFF |
| `--no-fgi` | Disable FGI contrarian replay | OFF |
| `--no-listings` | Disable listing_pump replay | OFF |
| `--no-stable-flow` | Disable stablecoin flow replay | OFF |
| `--no-chain-flow` | Disable TVL flow replay | OFF |
| `--no-funding-carry` | Disable cross-sectional funding carry | OFF |
| `--liq-cascade` | Enable liquidation cascade (default OFF; matches prod env var) | OFF |
| `--liq-min-usd` | Minimum 5min liq USD to trigger cascade | `1500000` |
| `--no-fast-cut` | Disable fast-cut exit (ablation) | OFF |
| `--min-score` | Override MIN_SCORE_TO_TRADE for this run | `60` |
| `--split` | Out-of-sample: split date range into N windows; each runs independently | `1` |


---

## Deployment

**Local development:**
```bash
python -m src.engine.runner --paper
# or
python -m src.engine.runner --live --auto-balance
```

**Production (GCP VM):**

1. VM: `kaizen-prod` in `asia-east2-a` zone
2. User: `prateekjain`
3. Systemd units (auto-deployed):
   - `/etc/systemd/system/kaizen.service`: Main trading engine
   - `/etc/systemd/system/kaizen-watchdog.service`: Stop-loss watchdog

**Systemd unit (`kaizen.service`):**
```
ExecStart=/home/prateekjain/kaizen-trader/.venv/bin/python -m src.engine.runner \
  --auto-balance \
  --tick 60 \
  --confirm

Environment=PAPER_TRADING=false
EnvironmentFile=/home/prateekjain/kaizen-trader/.env
```

When `--auto-balance` is set, the runner fetches the live account USDT balance from Binance (or OKX) instead of using a hardcoded balance.

**GitHub Actions auto-deploy:**
```
On: push to main
1. SSH to kaizen-prod via IAP tunnel (Workload Identity Federation)
2. git fetch origin main && git reset --hard
3. pip install -q requirements.txt
4. Copy kaizen.service and kaizen-watchdog.service to /etc/systemd/system/
5. systemctl daemon-reload && systemctl restart kaizen kaizen-watchdog
6. Poll: is-active check on both units, fail if either is inactive
7. Tail journalctl on success
```

**To trigger a redeploy:** Push any commit to `main`. The GitHub Actions workflow auto-deploys within 60 seconds.

---

## Operations

**Check health (from any terminal):**
```bash
# SSH to VM
gcloud compute ssh kaizen-prod --zone=asia-east2-a --tunnel-through-iap

# View live logs
sudo journalctl -u kaizen --follow

# View watchdog logs
sudo journalctl -u kaizen-watchdog --follow

# Check systemd status
sudo systemctl status kaizen kaizen-watchdog
```

**Read the [LIVE] heartbeat:**

Every 60 seconds, the runner logs:
```
[LIVE] Bal:$10,234 | Pos:2 [ENJ long +1.2% | SOL long -0.5%] | Trades:47 (61%WR) | PnL:$234.56 | Ticks:2847 Sigs:19542 | API:$1.234/day
```

Legend:
- `Bal`: Current account balance
- `Pos:N`: Number of open positions (list with side + unrealized P&L%)
- `Trades`: Total trades executed, win rate %
- `PnL`: Total session P&L in USDT
- `Ticks`: Number of 60s brain ticks executed (should increment every 60s)
- `Sigs`: Total signals received
- `API`: Estimated daily cost (ClaudeBrain only; RuleBrain is $0)

If `Ticks` stops incrementing, the engine has hung. The watchdog detects a stale heartbeat (>180s) and the root cron restarts the service.

**Find why a trade was BLOCKED:**

Search logs for `BLOCKED`:
```bash
sudo journalctl -u kaizen | grep BLOCKED
```

Example outputs:
```
BLOCKED BUY ENJ (symbol loss cooldown active)  — per-symbol 4h cooldown after 2 losses
BLOCKED BUY SOL (strategy 'funding_squeeze' loss cooldown)  — per-strategy 30min after 3 losses
BLOCKED BUY BTC (max positions reached)
BLOCKED BUY ETH (>80% balance deployed)
```

**Manual order placement (if brain is broken):**

The executor also has an isolated `open_position(decision)` entry point. In emergencies, you can SSH and:
```bash
python3 -c "
from src.engine.executor import Executor
from src.engine.claude_brain import TradeDecision
import time

executor = Executor(paper=False)
decision = TradeDecision(
    action='BUY', symbol='ENJ', side='long',
    size_usd=15, entry_price=0,
    stop_pct=0.10, target_pct=0.25,
    confidence='high', reasoning='manual emergency entry',
    signal_id=f'manual-{int(time.time())}',
    timestamp=time.time() * 1000,
)
pos = executor.open_position(decision)
print(f'Opened: {pos}')
"
```

**Graceful shutdown:**

The systemd unit sends `SIGTERM`. The runner waits for all threads (brain, price updater, stats, opus analysis) to finish, saves memory/portfolio, then exits. Typical shutdown latency: <10 seconds.

---

## Project Layout

```
kaizen-trader/
├── src/
│   ├── engine/
│   │   ├── runner.py                 Entry point: `python -m src.engine.runner`
│   │   ├── rule_brain.py             RuleBrain: 12-factor scoring, zero API cost
│   │   ├── claude_brain.py           ClaudeBrain: Haiku scan + Sonnet validation
│   │   ├── signal_detector.py        Signal filter + rank (no LLM)
│   │   ├── executor.py               Position lifecycle, stop/target checks
│   │   ├── data_streams.py           11 free data stream ingesters
│   │   └── log.py                    Structured logging facade
│   │
│   ├── backtesting/
│   │   ├── live_replay.py            Honest backtest harness
│   │   ├── data_loader.py            Load historical klines
│   │   ├── funding_loader.py         Load historical funding rates
│   │   └── ...                       (12 loaders: fgi, listings, stablecoins, etc.)
│   │
│   ├── execution/
│   │   ├── providers.py              Binance + OKX order execution
│   │   └── ...
│   │
│   ├── risk/
│   │   ├── loss_cooldown.py          Per-symbol + per-strategy cooldown gates
│   │   └── protections.py            Drawdown halts, daily loss limits
│   │
│   ├── storage/
│   │   └── database.py               Convex persistence layer
│   │
│   └── utils/, indicators/, etc.
│
├── scripts/
│   ├── run_live_backtest.py          `python scripts/run_live_backtest.py --days 90`
│   └── ...
│
├── tests/
│   ├── test_executor_exits.py        20 unit tests for stop/target/timeout/trailing
│   ├── test_protections.py           Drawdown + cooldown logic
│   └── ...
│
├── deploy/
│   ├── kaizen.service                Main trading engine systemd unit
│   ├── kaizen-watchdog.service       Watchdog systemd unit
│   └── setup_service.sh              Service installer (legacy)
│
├── .github/workflows/
│   └── deploy.yml                    GitHub Actions auto-deploy on push to main
│
├── watchdog.py                       Stop-loss safety net (separate process)
├── requirements.txt                  Dependencies
├── .env.example                      Environment variable template
└── README.md                         This file
```

---

## Testing

**Unit tests for executor exits (20 tests):**
```bash
python -m pytest tests/test_executor_exits.py -v
```

Tests cover:
- Hard stop-loss (long + short)
- Take-profit (long + short)
- Timeout (>48h hold)
- Trailing stop activation and override of static stop
- Race condition guard (concurrent close attempts)

**Run all tests:**
```bash
python -m pytest tests/ -v
```

---

## Development Workflow

1. **Branch from main:**
   ```bash
   git checkout -b feature/my-new-strategy
   ```

2. **Make changes, run tests locally:**
   ```bash
   python -m pytest tests/ -v
   python -m src.engine.runner --paper  # manual smoke test
   ```

3. **Push and auto-deploy:**
   ```bash
   git push origin feature/my-new-strategy
   # ... open PR, merge to main
   ```

4. **GitHub Actions deploys automatically:**
   - SSH to `kaizen-prod` via IAP
   - `git reset --hard origin/main`
   - `pip install -r requirements.txt`
   - `systemctl restart kaizen kaizen-watchdog`
   - Verify both services are active
   - Tail journalctl to confirm Ticks are advancing

5. **Verify on prod:**
   ```bash
   gcloud compute ssh kaizen-prod --zone=asia-east2-a --tunnel-through-iap
   sudo journalctl -u kaizen -n 20 --no-pager
   # Look for: Ticks:123 (should increment every ~60s)
   ```

---

## Key Files and Entry Points

| File | Purpose |
|------|---------|
| `src/engine/runner.py` | `python -m src.engine.runner --paper` or `--live` |
| `src/engine/rule_brain.py` | RuleBrain: 12-factor scoring system |
| `src/engine/claude_brain.py` | ClaudeBrain: Haiku + Sonnet LLM decisions |
| `src/engine/executor.py` | Position management, stop/target checks, fills |
| `scripts/run_live_backtest.py` | `python scripts/run_live_backtest.py --days 90` |
| `watchdog.py` | `python watchdog.py` (stop-loss safety net) |
| `.env.example` | Copy to `.env` and fill in your API keys |
| `deploy/kaizen.service` | Main engine systemd unit |
| `deploy/kaizen-watchdog.service` | Watchdog systemd unit |
| `.github/workflows/deploy.yml` | Auto-deploy on push to main |

---

## License

MIT
