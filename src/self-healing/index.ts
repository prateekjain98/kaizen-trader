/**
 * Self-Healing Engine
 *
 * After each position closes, this module:
 *  1. Diagnoses WHY the trade lost (or won)
 *  2. Applies a targeted parameter adjustment to avoid the same mistake
 *  3. Logs the diagnosis for the Claude Code log-analysis loop
 *
 * Separately, `log-analyzer.ts` runs on a timer and calls Claude via the
 * Anthropic SDK to read the full trade history and suggest deeper improvements.
 */

import { randomUUID } from 'crypto';
import type { Position, ScannerConfig, TradeDiagnosis, LossReason, MarketPhase } from '../types.js';
import { CONFIG_BOUNDS } from '../config.js';
import { insertDiagnosis, snapshotConfig, log } from '../storage/database.js';

// ─── Bounds ────────────────────────────────────────────────────────────────

function clamp(value: number, key: keyof ScannerConfig): number {
  const bounds = CONFIG_BOUNDS[key];
  return Math.min(bounds[1], Math.max(bounds[0], value));
}

function adjust(config: ScannerConfig, key: keyof ScannerConfig, delta: number): void {
  const current = config[key] as number;
  (config as Record<string, number>)[key] = clamp(current + delta, key);
}

// ─── Diagnosis logic ───────────────────────────────────────────────────────

function classifyLossReason(p: Position): LossReason {
  const holdHours = (p.holdMs ?? (Date.now() - p.openedAt)) / 3_600_000;
  const pnlPct = p.pnlPct ?? 0;
  const momentumAtEntry = ((p.entryPrice - p.lowWatermark) / p.lowWatermark); // rough proxy

  if (momentumAtEntry > 0.08 && holdHours < 4) return 'entered_pump_top';
  if (holdHours < 2 && p.exitReason === 'trailing_stop') return 'stop_too_tight';
  if (holdHours > 20 && pnlPct < -0.05) return 'stop_too_wide';
  if (p.qualScore < 55) return 'low_qual_score';

  return 'unknown';
}

// ─── Parameter adaptation ──────────────────────────────────────────────────

function applyLossAdaptation(
  p: Position,
  reason: LossReason,
  config: ScannerConfig,
): { action: string; changes: Partial<ScannerConfig> } {
  const changes: Partial<ScannerConfig> = {};
  let action = 'no change';

  switch (reason) {
    case 'entered_pump_top': {
      const key = p.tier === 'swing' ? 'momentumPctSwing' : 'momentumPctScalp';
      const oldVal = config[key];
      adjust(config, key, 0.01);
      const newVal = config[key];
      changes[key] = newVal;
      action = `raise ${key} ${(oldVal * 100).toFixed(1)}% → ${(newVal * 100).toFixed(1)}%`;
      break;
    }
    case 'stop_too_tight': {
      const key = p.tier === 'swing' ? 'baseTrailPctSwing' : 'baseTrailPctScalp';
      const oldVal = config[key];
      adjust(config, key, 0.01);
      const newVal = config[key];
      changes[key] = newVal;
      action = `widen ${key} ${(oldVal * 100).toFixed(0)}% → ${(newVal * 100).toFixed(0)}%`;
      break;
    }
    case 'stop_too_wide': {
      const key = p.tier === 'swing' ? 'baseTrailPctSwing' : 'baseTrailPctScalp';
      const oldVal = config[key];
      adjust(config, key, -0.01);
      const newVal = config[key];
      changes[key] = newVal;
      action = `tighten ${key} ${(oldVal * 100).toFixed(0)}% → ${(newVal * 100).toFixed(0)}%`;
      break;
    }
    case 'low_qual_score': {
      const key = p.tier === 'swing' ? 'minQualScoreSwing' : 'minQualScoreScalp';
      const oldVal = config[key];
      adjust(config, key, 2);
      const newVal = config[key];
      changes[key] = newVal;
      action = `raise ${key} ${oldVal} → ${newVal}`;
      break;
    }
    case 'funding_squeeze': {
      const oldVal = config.fundingRateExtremeThreshold;
      adjust(config, 'fundingRateExtremeThreshold', -0.0001);
      const newVal = config.fundingRateExtremeThreshold;
      changes.fundingRateExtremeThreshold = newVal;
      action = `lower funding threshold ${(oldVal * 100).toFixed(3)}% → ${(newVal * 100).toFixed(3)}%`;
      break;
    }
    default: {
      action = 'no change — unknown loss reason';
      break;
    }
  }

  return { action, changes };
}

// ─── Public API ────────────────────────────────────────────────────────────

const MAX_ADAPTATIONS_PER_SESSION = 20;
let adaptationCount = 0;

export function onPositionClosed(
  p: Position,
  config: ScannerConfig,
  marketPhase: MarketPhase,
): void {
  const pnlPct = p.pnlPct ?? 0;
  const isLoss = pnlPct < -0.005; // >0.5% loss triggers diagnosis

  if (!isLoss) {
    log('heal', `${p.symbol} WIN +${(pnlPct * 100).toFixed(1)}% — no parameter changes`, {
      symbol: p.symbol, strategy: p.strategy,
    });
    return;
  }

  if (adaptationCount >= MAX_ADAPTATIONS_PER_SESSION) {
    log('warn', `Self-healer hit session cap (${MAX_ADAPTATIONS_PER_SESSION} adaptations) — skipping`, {
      symbol: p.symbol,
    });
    return;
  }

  const lossReason = classifyLossReason(p);
  const holdMs = (p.closedAt ?? Date.now()) - p.openedAt;
  const { action, changes } = applyLossAdaptation(p, lossReason, config);

  const diagnosis: TradeDiagnosis = {
    positionId: p.id,
    symbol: p.symbol,
    strategy: p.strategy,
    pnlPct,
    holdMs,
    exitReason: p.exitReason ?? 'error',
    lossReason,
    entryQualScore: p.qualScore,
    marketPhaseAtEntry: marketPhase,
    action,
    parameterChanges: changes,
    timestamp: Date.now(),
  };

  insertDiagnosis(diagnosis);
  snapshotConfig(config, `self-healer: ${action}`);
  adaptationCount++;

  log('heal', `${p.symbol} LOSS ${(pnlPct * 100).toFixed(1)}% reason=${lossReason} → ${action}`, {
    symbol: p.symbol, strategy: p.strategy,
    data: { lossReason, action, changes },
  });
}

export function resetSessionCount(): void {
  adaptationCount = 0;
}
