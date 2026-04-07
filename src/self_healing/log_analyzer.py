"""Claude-powered log analyzer — the core self-improving loop."""

import json
from dataclasses import asdict
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from src.config import env, CONFIG_BOUNDS, default_scanner_config
from src.storage.database import get_closed_trades, get_recent_diagnoses, get_recent_logs, snapshot_config, log
from src.evaluation.metrics import compute_metrics, format_metrics
from src.types import ScannerConfig


class StrategyInsight(BaseModel):
    strategy: str
    verdict: str
    observation: str
    recommendation: str


class ParameterChange(BaseModel):
    parameter: str
    currentValue: float
    proposedValue: float
    evidence: str
    confidence: str


class Analysis(BaseModel):
    chainOfThought: str
    summary: str
    topIssues: list[str]
    strategyInsights: list[StrategyInsight]
    parameterChanges: list[ParameterChange]
    newStrategySuggestions: list[str]
    overallHealthScore: int = Field(ge=0, le=100)


def _build_prompt(config: ScannerConfig) -> str:
    trades = get_closed_trades(300)
    diagnoses = get_recent_diagnoses(50)
    error_logs = [l for l in get_recent_logs(200) if l.level in ("error", "warn")][:30]
    metrics = compute_metrics(300)
    metrics_str = format_metrics(metrics)

    config_snapshot = asdict(config)
    recent_trades = [
        {
            "symbol": t.symbol, "strategy": t.strategy, "side": t.side,
            "tier": t.tier, "pnl_pct": f"{t.pnl_pct:.4f}" if t.pnl_pct else None,
            "hold_hours": f"{(t.closed_at - t.opened_at) / 3_600_000:.1f}" if t.closed_at else None,
            "exit_reason": t.exit_reason, "qual_score": t.qual_score,
        }
        for t in trades[:100]
    ]

    bounds_summary = "\n".join(f"  {k}: [{lo}, {hi}]" for k, (lo, hi) in CONFIG_BOUNDS.items())

    return f"""You are a quantitative trading analyst reviewing the performance of an autonomous crypto trading system.

Your job is to:
1. Reason through the data step by step (chain of thought)
2. Identify specific problems backed by evidence
3. Recommend targeted parameter changes with supporting data
4. Surface patterns that require new strategy logic

## Current Configuration
```json
{json.dumps(config_snapshot, indent=2)}
```

## Hard Parameter Bounds (you MUST stay within these)
{bounds_summary}

## Performance Metrics (last {metrics.total_trades} closed trades)
```
{metrics_str}
```

## Recent Trade History (last 100)
```json
{json.dumps(recent_trades, indent=2)}
```

## Self-Healer Diagnosis History (last 50 adaptations)
```json
{json.dumps([{"position_id": d.position_id, "symbol": d.symbol, "strategy": d.strategy, "pnl_pct": d.pnl_pct, "loss_reason": d.loss_reason, "action": d.action} for d in diagnoses[:50]], indent=2)}
```

## Recent Error/Warning Logs
{chr(10).join(f'[{l.level.upper()}] {("[" + l.symbol + "] ") if l.symbol else ""}{l.message}' for l in error_logs) or '(none)'}

---

## Instructions

Think through the data carefully before producing your output. Return a JSON object with this structure:

{{
  "chainOfThought": "Your step-by-step reasoning...",
  "summary": "2-3 sentence overall assessment",
  "topIssues": ["specific issue 1", ...],
  "strategyInsights": [{{"strategy": "...", "verdict": "performing_well|underperforming|needs_disable|needs_more_data", "observation": "...", "recommendation": "..."}}],
  "parameterChanges": [{{"parameter": "exactParameterName", "currentValue": 0.02, "proposedValue": 0.03, "evidence": "...", "confidence": "low|medium|high"}}],
  "newStrategySuggestions": ["..."],
  "overallHealthScore": 72
}}

IMPORTANT: Only recommend changes where data provides clear evidence. Stay within CONFIG_BOUNDS."""


def _apply_changes(config: ScannerConfig, changes: list[ParameterChange]) -> dict:
    applied = []
    rejected = []

    for change in changes:
        if change.confidence == "low":
            rejected.append(f"{change.parameter} — confidence too low")
            continue

        key = change.parameter
        bounds = CONFIG_BOUNDS.get(key)
        if not bounds:
            rejected.append(f"{change.parameter} — unknown parameter")
            continue

        if not isinstance(change.proposedValue, (int, float)):
            rejected.append(f"{change.parameter} — non-numeric value")
            continue

        lo, hi = bounds
        if change.proposedValue < lo or change.proposedValue > hi:
            rejected.append(f"{change.parameter}={change.proposedValue} — out of bounds [{lo}, {hi}]")
            continue

        old = getattr(config, key, None)
        if old is None:
            rejected.append(f"{change.parameter} — not found on config")
            continue

        setattr(config, key, change.proposedValue)
        applied.append(f"{change.parameter}: {old} -> {change.proposedValue} ({change.evidence[:80]})")

    return {"applied": applied, "rejected": rejected}


def run_analysis(config: ScannerConfig) -> Optional[Analysis]:
    if not env.anthropic_api_key:
        log("warn", "Log analyzer skipped — ANTHROPIC_API_KEY not set")
        return None

    trade_count = len(get_closed_trades(1))
    if trade_count < env.min_trades_for_analysis:
        log("info", f"Log analyzer skipped — {trade_count}/{env.min_trades_for_analysis} trades needed")
        return None

    log("info", f"Running Claude analysis ({trade_count} closed trades)...")

    prompt = _build_prompt(config)
    client = anthropic.Anthropic(api_key=env.anthropic_api_key)

    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        block = message.content[0]
        if block.type != "text":
            log("error", "Log analyzer: unexpected response shape from Claude")
            return None
        raw_text = block.text
    except Exception as err:
        log("error", f"Log analyzer: Claude API error — {err}")
        return None

    # Parse JSON
    try:
        json_str = raw_text.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[1] if "\n" in json_str else json_str[3:]
        if json_str.endswith("```"):
            json_str = json_str[:-3]
        json_str = json_str.strip()
        parsed = json.loads(json_str)
    except Exception:
        log("error", "Log analyzer: failed to parse Claude response as JSON",
            data={"preview": raw_text[:300]})
        return None

    try:
        analysis = Analysis(**parsed)
    except Exception as err:
        log("error", f"Log analyzer: Claude response failed validation — {err}")
        return None

    result = _apply_changes(config, analysis.parameterChanges)

    if result["applied"] or result["rejected"]:
        snapshot_config(config, f"claude-analysis: {analysis.summary[:100]}")

    log("heal", f"Claude analysis complete — health={analysis.overallHealthScore}/100",
        data={
            "summary": analysis.summary,
            "top_issues": analysis.topIssues,
            "applied_changes": result["applied"],
            "rejected_changes": result["rejected"],
            "new_strategy_suggestions": analysis.newStrategySuggestions,
        })

    if result["applied"]:
        log("info", f"Applied {len(result['applied'])} parameter changes:\n  " + "\n  ".join(result["applied"]))

    return analysis
