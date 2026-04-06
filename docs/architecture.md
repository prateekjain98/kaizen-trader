# Architecture

## System overview

```mermaid
flowchart TD
    WS["**Coinbase WebSocket**\nprice ticks · L2 order book"]
    CP["CryptoPanic\nnews sentiment"]
    LC["LunarCrush\nsocial score + velocity"]
    WA["Whale Alert\nnet flow direction"]
    BN["Binance Futures\nfunding rates · liquidations"]
    DL["DeFiLlama\nprotocol revenue"]
    FG["Alternative.me\nFear & Greed index"]

    STRAT["**Strategy Scanners ×12**"]
    QUAL["**Qualification Scorer**\nbase + news + social + context + fear/greed\nscore ≥ threshold to proceed"]
    KELLY["**Kelly Position Sizer**\nquarter-Kelly × qual score multiplier"]
    CB["Circuit Breaker\n!circuitBreaker && openPos < max"]
    EXEC["**Executor**\nCoinbase Advanced REST (HMAC)\nor paper sim with slippage model"]

    L1["**Self-Healing Layer 1**\nper-loss rule-based correction\nclassifyLossReason → patch parameter\nmax 20 adaptations · bounded by CONFIG_BOUNDS"]
    L2["**Self-Healing Layer 2**\nperiodic Claude analysis\ncomputeMetrics → buildPrompt → claude-opus-4-6\nchain-of-thought → Zod-validated JSON\nmedium/high confidence only"]
    DB[("**SQLite · trader.db**\npositions · trades · logs\ndiagnoses · config_history")]

    WS --> STRAT
    CP & LC & WA & BN & DL & FG --> STRAT
    STRAT --> QUAL
    QUAL -->|passes| KELLY
    KELLY --> CB
    CB -->|allowed| EXEC
    EXEC -->|on close| L1
    L1 --> DB
    DB -->|every 60m| L2
    L2 --> DB
```

## Key design decisions

### Why two self-healing layers?

The rule-based healer (Layer 1) is fast and local — it fires immediately after every loss and patches one parameter. Think of it as a PID controller: it corrects the most recent error without seeing the broader pattern.

Claude (Layer 2) solves a different problem: patterns that only become visible across many trades. "Momentum scalp consistently loses on Monday mornings" or "funding_extreme trades placed during extreme greed underperform even when the signal is strong" — these require reasoning over a longer window than a rule-based system can handle efficiently.

Using both is the same principle as having automated tests plus a code reviewer: fast automated feedback for obvious issues, periodic deep review for structural problems.

### Why chain-of-thought prompting?

Early versions asked Claude directly for a parameter patch and got overconfident changes with thin reasoning. Adding chain-of-thought (asking Claude to reason before producing the JSON) improved patch quality significantly:

- Forces articulation of the evidence before the recommendation
- Catches logical gaps (a strategy with 3 losses isn't statistically significant)
- Produces audit-able reasoning stored in the `data` field of the heal log

### Why Zod schema validation on the Claude response?

LLM output is non-deterministic. A Zod schema gives us:
1. Type safety — the rest of the codebase can trust the shapes
2. Rejection of malformed responses with clear error messages
3. A spec for what we expect (if Claude drifts, we see it immediately)

### Why SQLite and not a hosted database?

The full audit trail (every trade, every diagnosis, every config snapshot) needs to be queryable by Claude Code locally. SQLite is zero-infrastructure, ships as an npm package, and the `analyze-logs.ts` script can query it directly without a network call. The `CLAUDE.md` instructions use raw `sqlite3` CLI commands that work on any machine.

### Why Kelly criterion for position sizing?

Fixed position sizing (e.g., always $100) is common but ignores strategy quality. A strategy with a 70% win rate and 2:1 win/loss ratio should get more capital than one with a 45% win rate and 1:1 ratio.

Quarter-Kelly specifically is used instead of full Kelly because:
- Full Kelly requires precise win rate estimation, which requires large sample sizes
- Quarter-Kelly gives similar long-term growth with significantly lower variance
- At small trade counts, it's effectively equivalent to conservative fixed-fractional

See `src/risk/position-sizer.ts` for the full implementation.

## Signal pipeline

The qualification scorer aggregates five independent signal sources to avoid over-reliance on any single signal:

| Source | Weight | Notes |
|---|---|---|
| Strategy score | 50% | From the strategy scanner itself |
| News sentiment | 15% | CryptoPanic headline + vote analysis |
| Social momentum | 15% | LunarCrush galaxy score + velocity |
| Market context | 10% | Phase (bull/bear/neutral), BTC dominance |
| Fear & Greed alignment | 10% | Directional agreement with trade side |

The independence of these signals is important. News sentiment and social momentum can be correlated (the same event drives both), but they're different enough to be worth separate weight. Market context and Fear & Greed are macro signals that are largely uncorrelated with token-specific price action.

## Data flow on a single tick

```mermaid
sequenceDiagram
    participant WS as Coinbase WS
    participant Momentum as momentum.ts
    participant OB as orderbook-imbalance.ts
    participant Scanner as scanMomentum()
    participant Scorer as scorer.qualify()
    participant Portfolio as portfolio.canOpen()
    participant Sizer as kellySize()
    participant Exec as paperBuy()
    participant DB as SQLite

    WS->>Momentum: tick SOL-USD @ $182.50 vol=2.4x
    WS->>OB: L2 book update bids/asks
    Momentum->>Scanner: rolling buffer updated
    Scanner-->>Scorer: TradeSignal score=68 momentum_swing
    Note over Scorer: base=68 + news+4 + social+3 + ctx-2 = 73 ≥ 55 ✓
    Scorer-->>Portfolio: qualified score=73
    Note over Portfolio: circuitBreaker=false, openPos=1/5 ✓
    Portfolio-->>Sizer: approved
    Note over Sizer: winRate=0.62, b=1.8 → quarter-Kelly → $91
    Sizer-->>Exec: place $91 buy
    Note over Exec: fill @ 182.59 (0.05% slip)
    Exec->>DB: INSERT position + trade log
```
