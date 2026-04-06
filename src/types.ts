// ─── Enums ────────────────────────────────────────────────────────────────

export type Side = 'long' | 'short';
export type Tier = 'scalp' | 'swing' | 'position';
export type MarketPhase = 'bull' | 'bear' | 'neutral' | 'extreme_fear' | 'extreme_greed';
export type ExitReason =
  | 'trailing_stop'
  | 'take_profit'
  | 'time_limit'
  | 'circuit_breaker'
  | 'manual'
  | 'error';

export type StrategyId =
  // v1 strategies (ported from original)
  | 'momentum_swing'
  | 'momentum_scalp'
  | 'listing_pump'
  | 'whale_accumulation'
  | 'token_unlock_short'
  // New strategies (v2)
  | 'mean_reversion'
  | 'funding_extreme'
  | 'liquidation_cascade'
  | 'orderbook_imbalance'
  | 'narrative_momentum'
  | 'correlation_break'
  | 'smart_money_follow'
  | 'protocol_revenue'
  | 'fear_greed_contrarian'
  | 'cross_exchange_divergence';

export type SignalSource =
  | 'news'
  | 'social'
  | 'whale_alert'
  | 'on_chain'
  | 'listing_detector'
  | 'funding_rates'
  | 'orderbook'
  | 'fear_greed'
  | 'protocol_revenue'
  | 'price_action'
  | 'correlation'
  | 'liquidation_data';

// ─── Signal ───────────────────────────────────────────────────────────────

export interface TradeSignal {
  id: string;
  symbol: string;
  productId: string; // e.g. "BTC-USD"
  strategy: StrategyId;
  side: Side;
  tier: Tier;
  score: number; // 0–100
  confidence: 'low' | 'medium' | 'high';
  sources: SignalSource[];
  reasoning: string;
  entryPrice: number;
  targetPrice?: number;   // optional take-profit
  stopPrice?: number;     // suggested initial stop
  suggestedSizeUsd?: number;
  expiresAt: number;      // unix ms — signal is stale after this
  createdAt: number;
}

// ─── Position ─────────────────────────────────────────────────────────────

export interface Position {
  id: string;
  symbol: string;
  productId: string;
  strategy: StrategyId;
  side: Side;
  tier: Tier;
  entryPrice: number;
  quantity: number;
  sizeUsd: number;
  openedAt: number;

  // Risk management state
  highWatermark: number;
  lowWatermark: number;
  currentPrice: number;
  trailPct: number;
  stopPrice: number;
  maxHoldMs: number;

  // Scores at entry
  qualScore: number;
  signalId: string;

  // Outcome (filled on close)
  status: 'open' | 'closing' | 'closed';
  exitPrice?: number;
  closedAt?: number;
  pnlUsd?: number;
  pnlPct?: number;
  exitReason?: ExitReason;

  paperTrading: boolean;
}

// ─── Trade (execution record) ─────────────────────────────────────────────

export interface Trade {
  id: string;
  positionId: string;
  side: 'buy' | 'sell';
  symbol: string;
  quantity: number;
  sizeUsd: number;
  price: number;
  orderId?: string;
  status: 'filled' | 'paper' | 'failed';
  error?: string;
  paperTrading: boolean;
  placedAt: number;
}

// ─── Self-healing ─────────────────────────────────────────────────────────

export type LossReason =
  | 'entered_pump_top'
  | 'stop_too_tight'
  | 'stop_too_wide'
  | 'low_qual_score'
  | 'adverse_news'
  | 'wrong_market_phase'
  | 'correlation_failure'
  | 'funding_squeeze'
  | 'liquidation_cascade_against'
  | 'repeated_symbol_loss'
  | 'unknown';

export interface TradeDiagnosis {
  positionId: string;
  symbol: string;
  strategy: StrategyId;
  pnlPct: number;
  holdMs: number;
  exitReason: ExitReason;
  lossReason: LossReason;
  entryQualScore: number;
  marketPhaseAtEntry: MarketPhase;
  action: string; // human-readable description of parameter change
  parameterChanges: Partial<ScannerConfig>;
  timestamp: number;
}

// ─── Config ───────────────────────────────────────────────────────────────

export interface ScannerConfig {
  // Momentum
  momentumPctSwing: number;       // default 0.02
  momentumPctScalp: number;       // default 0.025
  volumeMultiplierSwing: number;  // default 2.0
  volumeMultiplierScalp: number;  // default 2.5
  lookbackMsSwing: number;        // default 3_600_000 (1h)
  lookbackMsScalp: number;        // default 300_000 (5m)
  cooldownMsSwing: number;        // default 43_200_000 (12h)
  cooldownMsScalp: number;        // default 1_200_000 (20m)

  // Mean reversion
  vwapDeviationPct: number;       // default 0.03
  rsiOversold: number;            // default 30
  rsiOverbought: number;          // default 70

  // Qualification
  minQualScoreSwing: number;      // default 55
  minQualScoreScalp: number;      // default 45

  // Risk
  baseTrailPctSwing: number;      // default 0.07
  baseTrailPctScalp: number;      // default 0.04
  maxTrailPct: number;            // default 0.20
  maxHoldMsSwing: number;         // default 43_200_000 (12h)
  maxHoldMsScalp: number;         // default 7_200_000 (2h)

  // Funding strategy
  fundingRateExtremeThreshold: number;  // default 0.001 (0.1%)

  // Narrative strategy
  narrativeVelocityThreshold: number;   // default 3.0 (3x spike)

  // General
  maxWatchlist: number;           // default 50
}

// ─── Market Context ───────────────────────────────────────────────────────

export interface MarketContext {
  phase: MarketPhase;
  btcDominance: number;
  fearGreedIndex: number;
  totalMarketCapChangeD1: number;
  timestamp: number;
}

// ─── Log entry ────────────────────────────────────────────────────────────

export type LogLevel = 'info' | 'signal' | 'trade' | 'heal' | 'error' | 'warn';

export interface LogEntry {
  id: string;
  level: LogLevel;
  message: string;
  symbol?: string;
  strategy?: StrategyId;
  data?: Record<string, unknown>;
  ts: number;
}
