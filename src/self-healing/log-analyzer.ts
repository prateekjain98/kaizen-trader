/**
 * Claude-Powered Log Analyzer — the "self-healing via Claude Code" core loop.
 *
 * Every N minutes (default: 60), this module:
 *  1. Reads the last M closed trades + all diagnoses from the DB
 *  2. Sends them to Claude with a structured prompt
 *  3. Claude returns a JSON patch of parameter recommendations + reasoning
 *  4. We validate the patch against CONFIG_BOUNDS and apply it
 *  5. Everything is logged so the next analysis builds on it
 *
 * Run standalone:  tsx scripts/analyze-logs.ts
 * Or scheduled:    setInterval(runAnalysis, env.logAnalysisIntervalMins * 60_000)
 */

import Anthropic from '@anthropic-ai/sdk';
import { z } from 'zod';
import { env } from '../config.js';
import { CONFIG_BOUNDS, defaultScannerConfig } from '../config.js';
import { getClosedTrades, getRecentDiagnoses, getRecentLogs, snapshotConfig, log } from '../storage/database.js';
import type { ScannerConfig } from '../types.js';

// ─── Response schema ───────────────────────────────────────────────────────

const StrategyInsightSchema = z.object({
  strategy: z.string(),
  winRate: z.number().optional(),
  avgPnlPct: z.number().optional(),
  observation: z.string(),
  recommendation: z.string(),
});

const ParameterPatchSchema = z.record(z.string(), z.number());

const AnalysisResponseSchema = z.object({
  summary: z.string(),
  topIssues: z.array(z.string()),
  strategyInsights: z.array(StrategyInsightSchema),
  parameterPatch: ParameterPatchSchema,
  newStrategySuggestions: z.array(z.string()),
  confidenceLevel: z.enum(['low', 'medium', 'high']),
});

type AnalysisResponse = z.infer<typeof AnalysisResponseSchema>;

// ─── Prompt builder ───────────────────────────────────────────────────────

function buildPrompt(
  trades: ReturnType<typeof getClosedTrades>,
  diagnoses: ReturnType<typeof getRecentDiagnoses>,
  recentLogs: ReturnType<typeof getRecentLogs>,
  currentConfig: ScannerConfig,
): string {
  const tradeStats = trades.reduce<Record<string, { wins: number; losses: number; totalPnl: number }>>((acc, t) => {
    if (!acc[t.strategy]) acc[t.strategy] = { wins: 0, losses: 0, totalPnl: 0 };
    const s = acc[t.strategy]!;
    const pnl = t.pnlPct ?? 0;
    if (pnl > 0) s.wins++; else s.losses++;
    s.totalPnl += pnl;
    return acc;
  }, {});

  const errorLogs = recentLogs
    .filter(l => l.level === 'error' || l.level === 'warn')
    .slice(0, 20)
    .map(l => `[${l.level}] ${l.message}`)
    .join('\n');

  return `You are analyzing a self-healing crypto trading system. Your job is to review the trading history and recommend specific parameter improvements.

## Current Configuration
${JSON.stringify(currentConfig, null, 2)}

## Parameter Bounds (hard limits you MUST stay within)
${JSON.stringify(CONFIG_BOUNDS, null, 2)}

## Trade Statistics by Strategy (last ${trades.length} closed trades)
${JSON.stringify(tradeStats, null, 2)}

## Recent Closed Trades (last 50)
${JSON.stringify(trades.slice(0, 50).map(t => ({
  symbol: t.symbol,
  strategy: t.strategy,
  side: t.side,
  tier: t.tier,
  pnlPct: t.pnlPct?.toFixed(4),
  holdMs: t.closedAt ? t.closedAt - t.openedAt : null,
  exitReason: t.exitReason,
  qualScore: t.qualScore,
})), null, 2)}

## Recent Self-Healer Diagnoses (last 20)
${JSON.stringify(diagnoses.slice(0, 20), null, 2)}

## Recent Error/Warning Logs
${errorLogs || '(none)'}

## Your Task
Analyze this data and return a JSON object with this exact structure:

{
  "summary": "2-3 sentence overall assessment",
  "topIssues": ["issue 1", "issue 2", ...],  // up to 5 specific problems
  "strategyInsights": [
    {
      "strategy": "strategy_id",
      "winRate": 0.0–1.0,
      "avgPnlPct": number,
      "observation": "what you see",
      "recommendation": "what to change"
    }
  ],
  "parameterPatch": {
    // Only include parameters you want to change. Must stay within CONFIG_BOUNDS.
    "momentumPctSwing": 0.03,  // example
    ...
  },
  "newStrategySuggestions": ["suggestion 1", ...],  // ideas for new strategies based on patterns
  "confidenceLevel": "low" | "medium" | "high"
}

IMPORTANT:
- Only recommend parameter changes where you have clear evidence from the data
- Do not change parameters without clear reasoning
- Stay strictly within CONFIG_BOUNDS for all numeric values
- If a strategy is performing well, say so and don't change its parameters
- Return ONLY valid JSON, no markdown, no explanation outside the JSON`;
}

// ─── Apply validated patch ────────────────────────────────────────────────

function applyPatch(config: ScannerConfig, patch: Record<string, number>): { applied: string[]; rejected: string[] } {
  const applied: string[] = [];
  const rejected: string[] = [];

  for (const [rawKey, rawValue] of Object.entries(patch)) {
    const key = rawKey as keyof ScannerConfig;
    const bounds = CONFIG_BOUNDS[key];

    if (!bounds) {
      rejected.push(`${key} — unknown parameter`);
      continue;
    }

    if (typeof rawValue !== 'number' || isNaN(rawValue)) {
      rejected.push(`${key} — non-numeric value: ${String(rawValue)}`);
      continue;
    }

    if (rawValue < bounds[0] || rawValue > bounds[1]) {
      rejected.push(`${key}=${rawValue} out of bounds [${bounds[0]}, ${bounds[1]}]`);
      continue;
    }

    const old = config[key];
    (config as Record<string, number>)[key] = rawValue;
    applied.push(`${key}: ${String(old)} → ${rawValue}`);
  }

  return { applied, rejected };
}

// ─── Main analysis loop ───────────────────────────────────────────────────

export async function runAnalysis(config: ScannerConfig): Promise<AnalysisResponse | null> {
  if (!env.anthropicApiKey) {
    log('warn', 'Log analyzer skipped — ANTHROPIC_API_KEY not set');
    return null;
  }

  const closedTrades = getClosedTrades(200);
  if (closedTrades.length < env.minTradesForAnalysis) {
    log('info', `Log analyzer skipped — only ${closedTrades.length}/${env.minTradesForAnalysis} trades`);
    return null;
  }

  log('info', `Running Claude log analysis on ${closedTrades.length} trades...`);

  const diagnoses = getRecentDiagnoses(50);
  const recentLogs = getRecentLogs(100);
  const prompt = buildPrompt(closedTrades, diagnoses, recentLogs, config);

  const client = new Anthropic({ apiKey: env.anthropicApiKey });

  let rawResponse: string;
  try {
    const message = await client.messages.create({
      model: 'claude-opus-4-6',
      max_tokens: 2048,
      messages: [{ role: 'user', content: prompt }],
    });

    const block = message.content[0];
    if (!block || block.type !== 'text') {
      log('error', 'Log analyzer: unexpected response shape from Claude');
      return null;
    }
    rawResponse = block.text;
  } catch (err) {
    log('error', `Log analyzer: Claude API error — ${String(err)}`);
    return null;
  }

  // Parse and validate
  let parsed: unknown;
  try {
    // Strip any markdown code fences if present
    const jsonStr = rawResponse.replace(/^```(?:json)?\n?/m, '').replace(/\n?```$/m, '');
    parsed = JSON.parse(jsonStr);
  } catch {
    log('error', 'Log analyzer: failed to parse Claude response as JSON', {
      data: { preview: rawResponse.slice(0, 200) },
    });
    return null;
  }

  const result = AnalysisResponseSchema.safeParse(parsed);
  if (!result.success) {
    log('error', 'Log analyzer: Claude response failed schema validation', {
      data: { errors: result.error.flatten() },
    });
    return null;
  }

  const analysis = result.data;

  // Apply parameter patch
  const { applied, rejected } = applyPatch(config, analysis.parameterPatch);

  snapshotConfig(config, `claude-analysis: ${analysis.summary.slice(0, 80)}`);

  log('heal', `Claude analysis complete (confidence=${analysis.confidenceLevel})`, {
    data: {
      summary: analysis.summary,
      topIssues: analysis.topIssues,
      applied,
      rejected,
      newStrategySuggestions: analysis.newStrategySuggestions,
    },
  });

  if (applied.length > 0) {
    log('info', `Applied ${applied.length} parameter changes from Claude analysis:\n  ${applied.join('\n  ')}`);
  }
  if (rejected.length > 0) {
    log('warn', `Rejected ${rejected.length} parameter changes (out of bounds or unknown):\n  ${rejected.join('\n  ')}`);
  }

  return analysis;
}
