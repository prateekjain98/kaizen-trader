# Ralph Loop — kaizen-trader Self-Improving Loop

A recurring autonomous loop that iteratively makes kaizen-trader better, with
hard discipline against fabricated edge.

> **Origin**: pattern named after Ralph Wiggum — runs the same checks over and
> over until something changes. Inspired by Ronin (the NSE/BSE trader), where
> earlier inflated CAGR claims were caught only by repeated honest re-validation.

---

## Why this exists

Real-money trading software has two failure modes that compound silently:

1. **Drift** — small bugs (wrong fee rate, lookahead bias, missed restart hook)
   accumulate; live PnL diverges from backtest "edge"
2. **Fabricated edge** — backtests show profitability that doesn't survive in
   prod because of structural fictions (intra-bar wicks counted as targets,
   missing slippage, same-day signal lookups, direction-blind sizing)

The ralph loop is the discipline that catches both. Every fire either ships
one provably-better commit or honestly reports the strategy isn't world-class
yet — never both.

---

## Constitution (read every fire, do not violate)

1. **No fabrication.** Every reported number comes from a real command output
   (`journalctl`, Binance API, `git log`, `pytest` stdout). If a number isn't
   available, write `UNAVAILABLE` — never estimate.
2. **No declaring success without proof.** "Best in the world" requires ALL
   gates GREEN. Any single RED → answer is "not yet, here's the gap."
3. **No silent shortcuts.** Declare every step skipped explicitly in the
   `SHORTCUTS TAKEN` section of the report.
4. **No flattery.** State the truth even if uncomfortable.
5. **Stop on regression.** If production is broken / restart-looping /
   deadlocked, FIX or REVERT before any feature work.

---

## How it runs

Currently scheduled as a session-only cron via the Claude `CronCreate` tool:

```
*/2 * * * *  (every 2 minutes)
```

Each fire executes the four steps below. Between fires, sub-agents are
dispatched in parallel on independent research / build tracks so the loop is
never idle on the wall clock.

When the Claude session ends, the cron dies. Re-create it with the same
2-minute cadence on session restart.

---

## STEP 1 — Production health (must be GREEN to proceed)

```bash
gcloud compute ssh kaizen-prod --zone=asia-east2-a --tunnel-through-iap --command='
  systemctl is-active kaizen kaizen-watchdog
  echo ===HB===
  sudo journalctl -u kaizen --since="3 min ago" --no-pager | grep "Bal:" | tail -3
  echo ===ERR===
  sudo journalctl -u kaizen --since="10 min ago" --no-pager \
    | grep -iE "Traceback|NameError|deadlock|fatal|restart counter" \
    | grep -v "\\-4120" | tail -5
  echo ===LIVENESS===
  sudo journalctl -t kaizen-liveness --since="30 min ago" --no-pager \
    | grep -iE "restart|stale" | tail -3
'
```

**Verify**: BOTH services active; `Ticks` counter advancing across last 2
heartbeats; no errors; no liveness restart in 30 min. If any FAIL: investigate
(`py-spy` if hung, `git log` for recent regressions), fix or revert. Do NOT
proceed to Step 2.

---

## STEP 2 — Compute the five gates (real data only)

| Gate | Source | PASS criterion |
|------|--------|----------------|
| **G1 Liveness** | `journalctl --since='24h ago' \| grep 'KAIZEN HUNG\|stale.*restart' \| wc -l` | == 0 |
| **G2 Trades 24h** | `journalctl --since='24h ago' \| grep -c '💰 OPEN'` | ≥ 3 |
| **G3 PnL 7d** | parse `💰 CLOSE ... \$([+-][0-9.]+)` and sum | ≥ 0 |
| **G4 Risk 7d** | count closes where `pnl_pct ≤ -10` | == 0 |
| **G5 Backtest** | `scripts/run_live_backtest.py` last run | exists + PnL ≥ 0 + replays LIVE filter chain |

Report each as `G1 PASS|FAIL|UNAVAILABLE: <number>`.

If ALL FIVE PASS → STEP 3 ADVERSARIAL CHECK before claiming.
If any FAIL → identify the SINGLE biggest gap and ship one improvement (STEP 4).

---

## STEP 3 — Adversarial check (only if all gates passed)

Spawn the `everything-claude-code:code-reviewer` agent with the prompt:

> "Steel-man the case that kaizen-trader is NOT the best crypto trader in the
> world. List concrete gaps."

Apply findings before claiming victory.

---

## STEP 4 — Implement one gap-closing change (only if not yet best)

Pick the SINGLE highest-impact failed gate. Hard rules:

- Run `everything-claude-code:code-reviewer` — fix every P0/P1 before commit
- For any new lock acquisition: `grep` callers; switch to `RLock` if any
  caller already holds the same lock (lost 48h to this on commit `13b6ba5`)
- `python3 -m pytest tests/test_executor_exits.py -x -q` must pass —
  show count line
- Import test: `python3 -c "from <module> import <newsymbol>"` — catches the
  NameError class that broke prod (commit `13b48c7`)
- Push, then VERIFY DEPLOY: ssh in, `git log --oneline -1` matches your hash,
  `Ticks` advanced on the new PID
- If anything regresses, REVERT immediately

---

## Constraints (every fire)

- Don't touch Convex code (deploy key missing on this laptop)
- Don't change `MAX_POSITION_USD` or `MAX_DAILY_LOSS_USD`
- ≤ 300 lines per iteration
- FIX-FIRST not feature when bot is broken
- Never claim "shipped" without verifying commit on VM AND `Ticks` advancing

---

## Parallelism

Between fires (and during long-running operations within a fire), dispatch
sub-agents on independent tracks. Each agent gets a self-contained prompt with
file paths, success criteria, and a deliverable spec. Examples used in this
session:

- Build offline replay of a specific live filter (oi_delta, basis, top_ls, cvd)
- Adversarial code review on prod executor for hidden edge leaks
- Walk-forward OOS validation of a candidate alpha
- Build a new historical loader (FGI, stablecoin flows, listings, top-movers)
- Verify each loader fetches REAL data (not stubbed)
- Hunt lookahead bias in event-stream timestamps
- Port prod exit policy (trail tiers + fast-cut) to backtest

Sub-agents leave changes uncommitted; the parent verifies + commits + pushes.

---

## Mandatory report format

```
HEALTH: <OK Ticks N→M | HUNG | RESTART_LOOP | ERRORED>
GATES:
  G1 Liveness:  <PASS|FAIL|UNAVAILABLE: N>
  G2 Trades:    <PASS|FAIL|UNAVAILABLE: N>
  G3 PnL 7d:    <PASS|FAIL|UNAVAILABLE: $X>
  G4 Risk 7d:   <PASS|FAIL|UNAVAILABLE: N>
  G5 Backtest:  <PASS|FAIL|UNAVAILABLE: state>
VERDICT: BEST IN WORLD? YES|NO
SHIPPED: <commit hash + 1 line | none — reason>
SHORTCUTS TAKEN: <list every step skipped | none>
NEXT GAP: <single highest-impact failure>
```

---

## How to run it

### Recreate the cron (session-only)

```python
CronCreate(
  cron="*/2 * * * *",
  recurring=True,
  prompt="<paste the body below>",
)
```

### The prompt body

The full RALPH LOOP prompt (the one each cron fire receives) is the body of
this document from `STEP 1` through `End response. No flattery.` plus the
explicit `REPO: /Users/prateekjain/Documents/Dev/kaizen-trader/` line and the
`PARALLEL: dispatch sub-agents on independent tracks. Don't sit idle between
fires.` directive.

The exact text (copy-pasteable) lives in this file's history under
`scripts/ralph_loop_prompt.txt`.

### Stop the loop

```python
CronList()                 # find the job ID
CronDelete(id="<id>")      # cancel
```

The cron always dies on Claude session restart anyway.

---

## What this loop has produced

Validated by real-money discipline (every metric below has a `git log` /
`journalctl` source):

- **Found and fixed a P0 fee bug** — `COMMISSION_PCT` was 0.075% (Binance
  spot BNB-discount) instead of the correct 0.04% Futures rate. PnL
  accounting was off by 1.875×. Commit `609b754`.
- **Unparalysed live trading** — `oi_delta` filter was blocking every brain
  decision because it required +3% OI movement on alts that rarely move that
  fast. Added extreme-funding bypass mirroring the existing `time_of_day`
  pattern. Commit `f2fa7ba`.
- **Ported 7 of 8 live entry filters to offline replay** — `time_of_day`,
  `correlation`, `volatility`, `oi_delta`, `basis`, `top_long_short_ratio`,
  `cvd_flow`. Only `liquidation_cascade` remains (no public historical feed).
- **Wired all 7 prod signal sources offline** — funding_squeeze, large_move,
  major_pump, fgi_contrarian, listing_pump (CoinGecko + Binance + Coinbase),
  funding_carry (cross-sectional), stable_flow (DefiLlama).
- **Found and fixed 17 backtest framework correctness bugs** including
  3 CRITICAL: direction-blind funding sizing, re-entry cooldown wall-clock
  fallback, stale CSV cache fabricating 50/50 taker volume splits.
- **Fixed 6 P1 prod bugs** — exit fee on entry notional, wrong balance field,
  short trail init, close uses stale price, non-atomic portfolio save,
  funding fees never settled.
- **Removed lookahead bias** — top_movers events stamped at `open_time`
  using `close_time` data (1h lookahead); stable_flow used same-day flow at
  start-of-day (24h lookahead); FGI same-day timestamp (≤60min lookahead).
  All shifted to honest causal timestamps.
- **Caught and retracted the funding_carry +$5.53 fiction** — the original
  90d × split=2 × 30 symbols → 107 trades / +$5.53 / "ROBUST" verdict was
  deconstructed within minutes of being deployed. Adversarial audit
  surfaced 3 compounding bugs (re-entry cooldown comparing wall-clock to
  sim time → 42 trades on 9 names back-to-back; total filter-chain bypass
  for funding_carry signals → no volatility/correlation/oi/basis/cvd/
  ls-crowding gates; 25/42 trades exited via fast_cut and only 1/42 hit
  target, so the +$5.70 W1 figure was wick-PnL not carry-PnL). Walk-forward
  OOS validation (60d IS / 30d OOS / 4 folds) independently confirmed:
  **0/4 folds profitable**, carry-attributable PnL **-$5.44 / 43 trades**,
  OOS Sharpe **-2.11** (vs literature's 0.5-1.5 "genuine edge" band). The
  prod-wiring (commit 0576eb8) was disabled by default (commit 0f5c698)
  before the next 8h boundary fired any live carry trades on the fictitious
  edge.

The loop's most important output is what it KILLED: the +$2.29 / +$2.82 /
+$9.20 / +$5.53 "ROBUST" results that earlier honest-but-incomplete
backtests reported. Each was a fiction the loop's discipline eventually
caught — sometimes within minutes of deployment, before any live capital
was at risk.

**Current honest verdict**: NO proven OOS edge in this strategy stack.
funding_squeeze (single-name, absolute rate) shows borderline positive
small-n results; cross-sectional funding_carry shows no OOS survival.
The path forward is fixing the 3 audit-identified bugs (cooldown clock,
filter-bypass scope, exit-attribution to thesis vs heuristics), then
re-running walk-forward — if PnL stays negative, the carry thesis
itself is wrong (not a coding bug), and the project pivots to a
different alpha source.
