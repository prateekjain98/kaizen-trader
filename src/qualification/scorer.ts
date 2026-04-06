/**
 * Multi-signal qualification scorer.
 *
 * Before any trade executes, this module aggregates all available signals
 * into a single 0–100 score. The strategy scanner generates a base score
 * from its own logic; this module applies adjustments from orthogonal signals.
 *
 * Signal weights (chosen for independence — low correlation between sources):
 *
 *   Base strategy score       50% weight  (0–100 from strategy scanner)
 *   News sentiment            15% weight  (-1 to +1 from CryptoPanic)
 *   Social momentum           15% weight  (galaxy score + velocity)
 *   Market context            10% weight  (phase, BTC dominance)
 *   Fear & Greed alignment    10% weight  (directional agreement with trade side)
 *
 * The final score must exceed the strategy's minQualScore threshold (config)
 * for the trade to proceed. This creates a two-gate system:
 *   1. Strategy logic generates the signal
 *   2. Orthogonal signals confirm or reject it
 *
 * "Two-gate" pattern learned from building LLM eval pipelines at Salesmonk —
 * the same principle that makes human-in-the-loop evals more reliable than
 * single-model scoring.
 */

import type { TradeSignal, MarketContext, ScannerConfig } from '../types.js';
import type { NewsSentiment } from '../signals/news.js';
import type { SocialSentiment } from '../signals/social.js';

export interface QualificationResult {
  score: number;
  passed: boolean;
  breakdown: {
    base: number;
    newsAdjustment: number;
    socialAdjustment: number;
    contextAdjustment: number;
    fearGreedAdjustment: number;
  };
  reasoning: string;
}

function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v));
}

// ─── News adjustment (-15 to +15) ────────────────────────────────────────────

function newsAdjustment(signal: TradeSignal, news: NewsSentiment | undefined): number {
  if (!news) return 0;
  const directionMatch = signal.side === 'long' ? news.score : -news.score;
  const velocity = news.velocityRatio > 2 ? Math.min(5, (news.velocityRatio - 2) * 2.5) : 0;
  return clamp(directionMatch * 12 + velocity, -15, 15);
}

// ─── Social adjustment (-12 to +12) ──────────────────────────────────────────

function socialAdjustment(signal: TradeSignal, social: SocialSentiment | undefined): number {
  if (!social) return 0;
  // Galaxy score 70+ = positive, 30- = negative
  const galaxyScore = (social.galaxyScore - 50) / 50 * 8;
  // Velocity spike
  const velocityBonus = social.velocityMultiple > 3 ? Math.min(4, (social.velocityMultiple - 3) * 2) : 0;
  const raw = signal.side === 'long' ? galaxyScore + velocityBonus : -(galaxyScore + velocityBonus);
  return clamp(raw, -12, 12);
}

// ─── Market context adjustment (-10 to +10) ──────────────────────────────────

function contextAdjustment(signal: TradeSignal, ctx: MarketContext): number {
  let adj = 0;
  switch (ctx.phase) {
    case 'bull':         adj = signal.side === 'long' ? 8  : -5;  break;
    case 'bear':         adj = signal.side === 'long' ? -8 : 8;   break;
    case 'extreme_greed': adj = signal.side === 'long' ? -5 : 5;  break;
    case 'extreme_fear':  adj = signal.side === 'long' ? 3  : -3; break;
    case 'neutral':      adj = 0; break;
  }
  // BTC dominance rising = altcoins weaker
  if (ctx.btcDominance > 55 && signal.side === 'long' && signal.symbol !== 'BTC') adj -= 3;
  return clamp(adj, -10, 10);
}

// ─── Fear & Greed alignment (-8 to +8) ───────────────────────────────────────

function fearGreedAdjustment(signal: TradeSignal, fgi: number): number {
  // Long + fear = contrarian alignment = small bonus
  // Long + greed = trend-following in euphoria = small penalty
  if (signal.side === 'long') {
    if (fgi < 30) return 6;   // fear = buy dips
    if (fgi > 75) return -5;  // greed = risky entry
  } else {
    if (fgi > 70) return 6;   // greed = short euphoria
    if (fgi < 25) return -5;  // fear = risky short
  }
  return 0;
}

// ─── Main scorer ──────────────────────────────────────────────────────────────

export function qualify(
  signal: TradeSignal,
  ctx: MarketContext,
  config: ScannerConfig,
  news?: NewsSentiment,
  social?: SocialSentiment,
): QualificationResult {
  const newsAdj    = newsAdjustment(signal, news);
  const socialAdj  = socialAdjustment(signal, social);
  const ctxAdj     = contextAdjustment(signal, ctx);
  const fgiAdj     = fearGreedAdjustment(signal, ctx.fearGreedIndex);

  const rawScore = signal.score + newsAdj + socialAdj + ctxAdj + fgiAdj;
  const score = clamp(rawScore, 0, 100);

  const minScore = signal.tier === 'scalp' ? config.minQualScoreScalp : config.minQualScoreSwing;
  const passed = score >= minScore;

  const parts = [
    `base=${signal.score}`,
    newsAdj   !== 0 ? `news${newsAdj > 0 ? '+' : ''}${newsAdj.toFixed(0)}` : null,
    socialAdj !== 0 ? `social${socialAdj > 0 ? '+' : ''}${socialAdj.toFixed(0)}` : null,
    ctxAdj    !== 0 ? `ctx${ctxAdj > 0 ? '+' : ''}${ctxAdj.toFixed(0)}` : null,
    fgiAdj    !== 0 ? `fgi${fgiAdj > 0 ? '+' : ''}${fgiAdj.toFixed(0)}` : null,
    `= ${score.toFixed(0)} (min ${minScore})`,
  ].filter(Boolean);

  return {
    score,
    passed,
    breakdown: { base: signal.score, newsAdjustment: newsAdj, socialAdjustment: socialAdj, contextAdjustment: ctxAdj, fearGreedAdjustment: fgiAdj },
    reasoning: parts.join(' '),
  };
}
