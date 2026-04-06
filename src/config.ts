import * as dotenv from 'dotenv';
import type { ScannerConfig } from './types.js';

dotenv.config();

function requireEnv(key: string): string {
  const v = process.env[key];
  if (!v) throw new Error(`Missing required env var: ${key}`);
  return v;
}

function optionalEnv(key: string): string | undefined {
  return process.env[key] || undefined;
}

function numEnv(key: string, fallback: number): number {
  const v = process.env[key];
  if (!v) return fallback;
  const n = Number(v);
  if (isNaN(n)) throw new Error(`Env var ${key} must be a number, got: ${v}`);
  return n;
}

function boolEnv(key: string, fallback: boolean): boolean {
  const v = process.env[key];
  if (!v) return fallback;
  return v.toLowerCase() === 'true' || v === '1';
}

export const env = {
  paperTrading: boolEnv('PAPER_TRADING', true),

  // Exchanges
  coinbaseApiKey: optionalEnv('COINBASE_API_KEY'),
  coinbaseApiSecret: optionalEnv('COINBASE_API_SECRET'),
  binanceApiKey: optionalEnv('BINANCE_API_KEY'),
  binanceApiSecret: optionalEnv('BINANCE_API_SECRET'),

  // AI
  anthropicApiKey: optionalEnv('ANTHROPIC_API_KEY'),

  // News & Social
  cryptoPanicToken: optionalEnv('CRYPTOPANIC_TOKEN'),
  serperApiKey: optionalEnv('SERPER_API_KEY'),
  twitterBearerToken: optionalEnv('TWITTER_BEARER_TOKEN'),
  lunarCrushApiKey: optionalEnv('LUNARCRUSH_API_KEY'),

  // On-chain
  whaleAlertApiKey: optionalEnv('WHALE_ALERT_API_KEY'),
  etherscanApiKey: optionalEnv('ETHERSCAN_API_KEY'),

  // Risk
  maxPositionUsd: numEnv('MAX_POSITION_USD', 100),
  maxDailyLossUsd: numEnv('MAX_DAILY_LOSS_USD', 300),
  maxOpenPositions: numEnv('MAX_OPEN_POSITIONS', 5),

  // Self-healing
  logAnalysisIntervalMins: numEnv('LOG_ANALYSIS_INTERVAL_MINS', 60),
  minTradesForAnalysis: numEnv('MIN_TRADES_FOR_ANALYSIS', 10),
} as const;

// ─── Default scanner config (mutable — self-healer patches this live) ──────

export const defaultScannerConfig: ScannerConfig = {
  // Momentum
  momentumPctSwing: 0.02,
  momentumPctScalp: 0.025,
  volumeMultiplierSwing: 2.0,
  volumeMultiplierScalp: 2.5,
  lookbackMsSwing: 3_600_000,
  lookbackMsScalp: 300_000,
  cooldownMsSwing: 43_200_000,
  cooldownMsScalp: 1_200_000,

  // Mean reversion
  vwapDeviationPct: 0.03,
  rsiOversold: 30,
  rsiOverbought: 70,

  // Qualification
  minQualScoreSwing: 55,
  minQualScoreScalp: 45,

  // Risk
  baseTrailPctSwing: 0.07,
  baseTrailPctScalp: 0.04,
  maxTrailPct: 0.20,
  maxHoldMsSwing: 43_200_000,
  maxHoldMsScalp: 7_200_000,

  // Funding strategy
  fundingRateExtremeThreshold: 0.001,

  // Narrative strategy
  narrativeVelocityThreshold: 3.0,

  // General
  maxWatchlist: 50,
};

// ─── Parameter bounds (self-healer can only move within these) ─────────────

export const CONFIG_BOUNDS: Record<keyof ScannerConfig, [number, number]> = {
  momentumPctSwing:              [0.01, 0.15],
  momentumPctScalp:              [0.015, 0.10],
  volumeMultiplierSwing:         [1.5, 5.0],
  volumeMultiplierScalp:         [1.5, 5.0],
  lookbackMsSwing:               [1_800_000, 14_400_000],
  lookbackMsScalp:               [60_000, 600_000],
  cooldownMsSwing:               [3_600_000, 86_400_000],
  cooldownMsScalp:               [300_000, 7_200_000],
  vwapDeviationPct:              [0.01, 0.10],
  rsiOversold:                   [20, 40],
  rsiOverbought:                 [60, 80],
  minQualScoreSwing:             [45, 85],
  minQualScoreScalp:             [35, 75],
  baseTrailPctSwing:             [0.04, 0.18],
  baseTrailPctScalp:             [0.02, 0.08],
  maxTrailPct:                   [0.10, 0.35],
  maxHoldMsSwing:                [14_400_000, 172_800_000],
  maxHoldMsScalp:                [1_800_000, 14_400_000],
  fundingRateExtremeThreshold:   [0.0005, 0.005],
  narrativeVelocityThreshold:    [1.5, 8.0],
  maxWatchlist:                  [10, 200],
};
