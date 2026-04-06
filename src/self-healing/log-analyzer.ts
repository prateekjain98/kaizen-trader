/**
 * Claude-powered log analyzer — the core self-improving loop.
 *
 * Design philosophy:
 *   The in-process self-healer (index.ts) handles fast, local corrections:
 *   one loss → one parameter patch. It's essentially a PID controller.
 *
 *   This module handles a fundamentally different problem: patterns that
 *   only emerge across many trades — strategy interactions, time-of-day
 *   effects, market regime correlations, signal quality drift.
 *
 *   Claude's reasoning is better suited for this than hand-coded heuristics.
 *   We give it institutional-quality metrics, raw trade data, and the full
 *   history of previous self-healer actions, then ask it to think in steps.
 *
 * Prompt design notes:
 *   - Chain-of-thought: Claude reasons before producing the JSON patch
 *   - Few-shot examples in the schema description prevent format errors
 *   - Hard bounds are included in the prompt (not just enforced in code)
 *   - We ask for confidence levels so low-confidence patches are discarded
 *   - "newStrategySuggestions" surfaces ideas we can implement as new files
 *
 * This is the same two-gate pattern we used at Salesmonk for LLM evals:
 * quantitative metrics gate + LLM reasoning gate, in sequence.
 */

import Anthropic from '@anthropic-ai/sdk';
import { z } from 'zod';
import { env } from '../config.js';
import { CONFIG_BOUNDS, defaultScannerConfig } from '../config.js';
import { getClosedTrades, getRecentDiagnoses, getRecentLogs, snapshotConfig, log } from '../storage/database.js';
import { computeMetrics, formatMetrics } from '../evaluation/metrics.js';
import type { ScannerConfig } from '../types.js';

// ─── Response schema ───────────────────────────────────────────────────────────

const StrategyInsightSchema = z.object({
  strategy:       z.string(),
  verdict:        z.enum(['performing_well', 'underperforming', 'needs_disable', 'needs_more_data']),
  observation:    z.string(),
  recommendation: z.string(),
});

const ParameterChangeSchema = z.object({
  parameter:    z.string(),
  currentValue: z.number(),
  proposedValue: z.number(),
  evidence:     z.string(), // what specifically in the data supports this change
  confidence:   z.enum(['low', 'medium', 'high']),
});

const AnalysisSchema = z.object({
  chainOfThought:         z.string(),    // Claude's reasoning before conclusions
  summary:                z.string(),
  topIssues:              z.array(z.string()),
  strategyInsights:       z.array(StrategyInsightSchema),
  parameterChanges:       z.array(ParameterChangeSchema),
  newStrategySuggestions: z.array(z.string()),
  overallHealthScore:     z.number().min(0).max(100),
});

type Analysis = z.infer<typeof AnalysisSchema>;

// ─── Prompt ───────────────────────────────────────────────────────────────────

function buildPrompt(config: ScannerConfig): string {
  const trades = getClosedTrades(300);
  const diagnoses = getRecentDiagnoses(50);
  const errorLogs = getRecentLogs(200).filter(l => l.level === 'error' || l.level === 'warn').slice(0, 30);
  const metrics = computeMetrics(300);
  const metricsStr = formatMetrics(metrics);

  const configSnapshot = Object.fromEntries(
    Object.entries(config).map(([k, v]) => [k, v])
  );

  const recentTrades = trades.slice(0, 100).map(t => ({
    symbol: t.symbol,
    strategy: t.strategy,
    side: t.side,
    tier: t.tier,
    pnlPct: t.pnlPct?.toFixed(4),
    holdHours: t.closedAt ? ((t.closedAt - t.openedAt) / 3_600_000).toFixed(1) : null,
    exitReason: t.exitReason,
    qualScore: t.qualScore,
    openedAt: t.openedAt ? new Date(t.openedAt).toISOString() : null,
  }));

  const parameterBoundsSummary = Object.entries(CONFIG_BOUNDS)
    .map(([k, [min, max]]) => `  ${k}: [${min}, ${max}]`)
    .join('\n');

  return `You are a quantitative trading analyst reviewing the performance of an autonomous crypto trading system.

Your job is to:
1. Reason through the data step by step (chain of thought)
2. Identify specific problems backed by evidence
3. Recommend targeted parameter changes with supporting data
4. Surface patterns that require new strategy logic

## Current Configuration
\`\`\`json
${JSON.stringify(configSnapshot, null, 2)}
\`\`\`

## Hard Parameter Bounds (you MUST stay within these)
${parameterBoundsSummary}

## Performance Metrics (last ${metrics.totalTrades} closed trades)
\`\`\`
${metricsStr}
\`\`\`

## Recent Trade History (last 100)
\`\`\`json
${JSON.stringify(recentTrades, null, 2)}
\`\`\`

## Self-Healer Diagnosis History (last 50 adaptations)
\`\`\`json
${JSON.stringify(diagnoses.slice(0, 50), null, 2)}
\`\`\`

## Recent Error/Warning Logs
${errorLogs.map(l => `[${l.level.toUpperCase()}] ${l.symbol ? '[' + l.symbol + '] ' : ''}${l.message}`).join('\n') || '(none)'}

---

## Instructions

Think through the data carefully before producing your output. Look for:

1. **Strategy-level patterns**: Is a specific strategy consistently losing? What's the common exit reason and hold time for its losses vs wins?

2. **Timing patterns**: Are losses clustered at specific hold durations (e.g., all scalp losses exit in <30m — stop too tight)?

3. **Over-correction risk**: The self-healer has already made adaptations. Look at the diagnosis history — has it been patching the same parameter repeatedly? That may indicate a deeper issue the rule-based healer can't see.

4. **Signal quality drift**: Are qual scores on losing trades near the minimum threshold? That suggests the threshold should be higher, or that certain signal combinations are low-quality.

5. **Interaction effects**: Do certain strategy + market phase combinations consistently underperform?

## Output Format

Return a JSON object with this exact structure (no markdown, no preamble):

{
  "chainOfThought": "Your step-by-step reasoning before conclusions...",
  "summary": "2-3 sentence overall assessment of system health",
  "topIssues": ["specific issue 1", "specific issue 2", ...],
  "strategyInsights": [
    {
      "strategy": "strategy_id",
      "verdict": "performing_well | underperforming | needs_disable | needs_more_data",
      "observation": "what the data shows",
      "recommendation": "specific action"
    }
  ],
  "parameterChanges": [
    {
      "parameter": "exactParameterName",
      "currentValue": 0.02,
      "proposedValue": 0.03,
      "evidence": "momentum_scalp has 8 losses where hold_hours < 0.5 — exit too fast suggests stop too tight",
      "confidence": "low | medium | high"
    }
  ],
  "newStrategySuggestions": [
    "Suggestion for a new strategy based on patterns in the data"
  ],
  "overallHealthScore": 72
}

IMPORTANT:
- Only recommend changes where the data provides clear evidence
- Do not change parameters with high confidence unless you have >15 data points
- If a strategy has <10 trades, verdict should be "needs_more_data"
- If the self-healer has already adjusted a parameter 3+ times, explain why your adjustment is different
- Stay strictly within CONFIG_BOUNDS`;
}

// ─── Apply validated parameter patch ─────────────────────────────────────────

function applyChanges(config: ScannerConfig, changes: Analysis['parameterChanges']): {
  applied: string[];
  rejected: string[];
} {
  const applied: string[] = [];
  const rejected: string[] = [];

  for (const change of changes) {
    // Skip low-confidence changes
    if (change.confidence === 'low') {
      rejected.push(`${change.parameter} — confidence too low (${change.confidence})`);
      continue;
    }

    const key = change.parameter as keyof ScannerConfig;
    const bounds = CONFIG_BOUNDS[key];

    if (!bounds) {
      rejected.push(`${change.parameter} — unknown parameter`);
      continue;
    }

    if (typeof change.proposedValue !== 'number' || isNaN(change.proposedValue)) {
      rejected.push(`${change.parameter} — non-numeric value`);
      continue;
    }

    if (change.proposedValue < bounds[0] || change.proposedValue > bounds[1]) {
      rejected.push(`${change.parameter}=${change.proposedValue} — out of bounds [${bounds[0]}, ${bounds[1]}]`);
      continue;
    }

    const old = config[key];
    (config as Record<string, number>)[key] = change.proposedValue;
    applied.push(`${change.parameter}: ${String(old)} → ${change.proposedValue} (${change.evidence.slice(0, 80)})`);
  }

  return { applied, rejected };
}

// ─── Main ─────────────────────────────────────────────────────────────────────

export async function runAnalysis(config: ScannerConfig): Promise<Analysis | null> {
  if (!env.anthropicApiKey) {
    log('warn', 'Log analyzer skipped — ANTHROPIC_API_KEY not set');
    return null;
  }

  const tradeCount = getClosedTrades(1).length;
  if (tradeCount < env.minTradesForAnalysis) {
    log('info', `Log analyzer skipped — ${tradeCount}/${env.minTradesForAnalysis} trades needed`);
    return null;
  }

  log('info', `Running Claude analysis (${tradeCount} closed trades)...`);

  const prompt = buildPrompt(config);
  const client = new Anthropic({ apiKey: env.anthropicApiKey });

  let rawText: string;
  try {
    const message = await client.messages.create({
      model: 'claude-opus-4-6',
      max_tokens: 4096,
      messages: [{ role: 'user', content: prompt }],
    });

    const block = message.content[0];
    if (!block || block.type !== 'text') {
      log('error', 'Log analyzer: unexpected response shape from Claude');
      return null;
    }
    rawText = block.text;
  } catch (err) {
    log('error', `Log analyzer: Claude API error — ${String(err)}`);
    return null;
  }

  // Parse — strip any accidental markdown fences
  let parsed: unknown;
  try {
    const jsonStr = rawText.replace(/^```(?:json)?\s*/m, '').replace(/\s*```$/m, '').trim();
    parsed = JSON.parse(jsonStr);
  } catch {
    log('error', 'Log analyzer: failed to parse Claude response as JSON', {
      data: { preview: rawText.slice(0, 300) },
    });
    return null;
  }

  const result = AnalysisSchema.safeParse(parsed);
  if (!result.success) {
    log('error', 'Log analyzer: Claude response failed Zod validation', {
      data: { errors: result.error.flatten() },
    });
    return null;
  }

  const analysis = result.data;

  // Apply changes (only medium/high confidence)
  const { applied, rejected } = applyChanges(config, analysis.parameterChanges);

  if (applied.length > 0 || rejected.length > 0) {
    snapshotConfig(config, `claude-analysis: ${analysis.summary.slice(0, 100)}`);
  }

  log('heal', `Claude analysis complete — health=${analysis.overallHealthScore}/100`, {
    data: {
      summary: analysis.summary,
      topIssues: analysis.topIssues,
      appliedChanges: applied,
      rejectedChanges: rejected,
      newStrategySuggestions: analysis.newStrategySuggestions,
    },
  });

  if (applied.length > 0) {
    log('info', `Applied ${applied.length} parameter changes:\n  ${applied.join('\n  ')}`);
  }

  return analysis;
}
