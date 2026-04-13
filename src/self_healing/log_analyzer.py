"""Claude-powered log analyzer — the core self-improving loop."""

import json
import math
from dataclasses import asdict
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from src.automation.github_issues import create_data_gap_issue
from src.config import env, CONFIG_BOUNDS
from src.self_healing.blind_spots import get_detector
from src.self_healing.chart_analyzer import render_chart, analyze_chart
from src.self_healing.delta_evaluator import get_evaluator
from src.self_healing.analysis_memory import get_analysis_memory
from src.storage.database import get_closed_trades, get_recent_diagnoses, get_recent_logs, snapshot_config, log
from src.evaluation.metrics import compute_metrics, format_metrics
from src.evaluation.strategy_selector import StrategySelector
from src.types import ScannerConfig

from dataclasses import fields as _dataclass_fields
_ALLOWED_CONFIG_KEYS = {f.name for f in _dataclass_fields(ScannerConfig)}


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


class DataSourceSuggestion(BaseModel):
    source: str
    rationale: str
    priority: str = "medium"  # "low" | "medium" | "high"


class Analysis(BaseModel):
    chainOfThought: str
    summary: str
    topIssues: list[str]
    strategyInsights: list[StrategyInsight]
    parameterChanges: list[ParameterChange]
    newStrategySuggestions: list[str]
    dataSourceSuggestions: list[DataSourceSuggestion] = Field(default_factory=list)
    overallHealthScore: int = Field(ge=0, le=100)


def _build_delta_section() -> str:
    """Build a summary of recent parameter deltas and their verdicts."""
    evaluator = get_evaluator()
    all_deltas = evaluator.get_all_deltas()
    if not all_deltas:
        return "(no parameter deltas recorded yet)"

    lines = []
    # Show most recent 15 deltas
    for d in all_deltas[-15:]:
        verdict_tag = d.verdict or d.evaluation_status
        before_wr = f"{d.trades_before.win_rate:.0%}" if d.trades_before else "?"
        after_wr = f"{d.trades_after.win_rate:.0%}" if d.trades_after else "pending"
        lines.append(
            f"- {d.parameter}: {d.old_value} -> {d.new_value} "
            f"[{verdict_tag}] (win_rate: {before_wr} -> {after_wr}, source={d.source})"
        )
    return "\n".join(lines)


def _build_strategy_health_section(selector: Optional[StrategySelector] = None) -> str:
    """Build a summary of strategy health from Darwinian selection."""
    if selector is None:
        return "(strategy selector not available)"

    health_list = selector.get_health_report()
    if not health_list:
        return "(no strategy health data yet — too few trades)"

    lines = []
    for h in sorted(health_list, key=lambda x: x.strategy_id):
        status = "enabled" if h.enabled else "DISABLED"
        sharpe_str = f"sharpe={h.rolling_sharpe:.2f}" if h.rolling_sharpe is not None else "sharpe=N/A"
        reason_str = f" reason={h.disable_reason}" if h.disable_reason else ""
        lines.append(
            f"- {h.strategy_id}: {status} | win_rate={h.rolling_win_rate:.0%} | "
            f"{sharpe_str} | consec_losses={h.consecutive_losses}{reason_str}"
        )
    return "\n".join(lines)


def _build_blind_spots_section() -> str:
    """Build a summary of detected blind spots."""
    flagged = get_detector().get_flagged_blind_spots()
    if not flagged:
        return "(none — all loss patterns classified)"
    lines = []
    for bs in flagged:
        lines.append(
            f"- {bs.key}: {bs.occurrences} occurrences, avg loss {bs.avg_pnl_pct*100:.1f}%, "
            f"hold_bucket={bs.hold_bucket}, market_phase={bs.market_phase}"
        )
    return "\n".join(lines)


def _build_prompt(config: ScannerConfig,
                  strategy_selector: Optional[StrategySelector] = None) -> str:
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

    delta_section = _build_delta_section()
    strategy_health_section = _build_strategy_health_section(strategy_selector)
    blind_spots_section = _build_blind_spots_section()

    return f"""You are a quantitative trading analyst reviewing the performance of an autonomous crypto trading system.

Your job is to:
1. Reason through the data step by step (chain of thought)
2. Identify specific problems backed by evidence
3. Recommend targeted parameter changes with supporting data
4. Surface patterns that require new strategy logic
5. Suggest missing data sources that could improve decisions

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
{json.dumps([{{"position_id": d.position_id, "symbol": d.symbol, "strategy": d.strategy, "pnl_pct": d.pnl_pct, "loss_reason": d.loss_reason, "action": d.action}} for d in diagnoses[:50]], indent=2)}
```

## Recent Error/Warning Logs
{chr(10).join(f'[{{l.level.upper()}}] {{("[" + l.symbol + "] ") if l.symbol else ""}}{{l.message}}' for l in error_logs) or '(none)'}

## Parameter Delta Tracking
{delta_section}

## Strategy Health (Darwinian Selection)
{strategy_health_section}

## Detected Blind Spots
{blind_spots_section}

## Prior Analysis Context (Working Memory)
{get_analysis_memory().get_working_context()}

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
  "dataSourceSuggestions": [{{"source": "name of data source", "rationale": "why this would help", "priority": "low|medium|high"}}],
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
        if key not in _ALLOWED_CONFIG_KEYS:
            rejected.append(f"{change.parameter} — not a recognized ScannerConfig field")
            continue
        bounds = CONFIG_BOUNDS.get(key)
        if not bounds:
            rejected.append(f"{change.parameter} — unknown parameter")
            continue

        if not isinstance(change.proposedValue, (int, float)) or not math.isfinite(change.proposedValue):
            rejected.append(f"{change.parameter} — non-numeric or non-finite value")
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

        # Record delta for tracking and auto-revert evaluation
        get_evaluator().record_delta(
            parameter=key, old_value=old, new_value=change.proposedValue,
            reason=change.evidence[:120], source="claude_analysis", config=config,
        )

    return {"applied": applied, "rejected": rejected}


def _adversarial_review(analysis: Analysis) -> list[str]:
    """Challenge proposed parameter changes with a skeptical second opinion.

    Returns a list of parameter names to REJECT (adversary had high confidence).
    Uses claude-sonnet-4-20250514 for cost efficiency.
    """
    if not analysis.parameterChanges or not env.anthropic_api_key:
        return []

    changes_desc = "\n".join(
        f"- {c.parameter}: {c.currentValue} -> {c.proposedValue} (evidence: {c.evidence})"
        for c in analysis.parameterChanges
    )

    prompt = f"""You are a skeptical risk manager reviewing proposed parameter changes to an autonomous crypto trading system.

## Proposed Changes
{changes_desc}

## System Context
Summary: {analysis.summary}
Health Score: {analysis.overallHealthScore}/100
Top Issues: {', '.join(analysis.topIssues[:3])}

For EACH proposed change, argue why it might be WRONG or harmful. Consider:
1. Is the evidence statistically significant or could it be noise?
2. Could the change have unintended side effects?
3. Is the sample size large enough to justify this change?
4. Could market regime shifts make this change counterproductive?

Return a JSON array where each element is:
{{"parameter": "name", "counter_argument": "why this change might be wrong", "rejection_confidence": "low|medium|high"}}

Only rate "high" confidence if you have a strong, specific reason the change would be harmful."""

    try:
        client = anthropic.Anthropic(api_key=env.anthropic_api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        block = message.content[0]
        if block.type != "text":
            return []

        raw = block.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]

        reviews = json.loads(raw.strip())
        rejected = []
        for review in reviews:
            if review.get("rejection_confidence") == "high":
                rejected.append(review["parameter"])
                log("info", f"Adversarial review REJECTED {review['parameter']}: {review.get('counter_argument', '')[:100]}")

        if rejected:
            log("info", f"Adversarial debate rejected {len(rejected)}/{len(analysis.parameterChanges)} proposed changes")
        else:
            log("info", "Adversarial debate: all proposed changes survived")

        return rejected
    except Exception as err:
        log("warn", f"Adversarial review failed (proceeding without): {err}")
        return []


def _ensemble_verify(prompt: str, primary_changes: list[ParameterChange],
                     client: anthropic.Anthropic) -> list[ParameterChange]:
    """Run 2 additional analyses with Sonnet at different temperatures.

    Only keep parameter changes from the primary analysis that also appear
    in at least 1 of the 2 ensemble runs (i.e., 2/3 majority).
    Uses Sonnet for cost efficiency (~10x cheaper than Opus).
    """
    if not primary_changes:
        return primary_changes

    ensemble_params: dict[str, int] = {}  # parameter -> vote count
    for c in primary_changes:
        ensemble_params[c.parameter] = 1  # primary vote

    temperatures = [0.3, 1.0]  # primary was ~0.7 (default)

    for temp in temperatures:
        try:
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                temperature=temp,
                messages=[{"role": "user", "content": prompt}],
            )
            block = message.content[0]
            if block.type != "text":
                continue

            raw = block.text.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]

            parsed = json.loads(raw.strip())
            ensemble_analysis = Analysis(**parsed)

            for c in ensemble_analysis.parameterChanges:
                if c.parameter in ensemble_params:
                    ensemble_params[c.parameter] += 1
        except Exception as err:
            log("warn", f"Ensemble run (temp={temp}) failed: {err}")
            # Failed run: do NOT grant bonus votes — require all successful
            # runs to agree, otherwise we bypass the filter entirely

    # Keep only changes with 2+ votes (appeared in primary + at least 1 ensemble)
    surviving = [c for c in primary_changes if ensemble_params.get(c.parameter, 0) >= 2]
    filtered_count = len(primary_changes) - len(surviving)

    if filtered_count:
        filtered_names = [c.parameter for c in primary_changes if ensemble_params.get(c.parameter, 0) < 2]
        log("info", f"Ensemble filter removed {filtered_count} changes (no majority): {', '.join(filtered_names)}")
    else:
        log("info", f"Ensemble filter: all {len(surviving)} changes confirmed by majority vote")

    return surviving


def run_analysis(config: ScannerConfig,
                  strategy_selector: Optional[StrategySelector] = None) -> Optional[Analysis]:
    if not env.anthropic_api_key:
        log("warn", "Log analyzer skipped — ANTHROPIC_API_KEY not set")
        return None

    trade_count = len(get_closed_trades(1))
    if trade_count < env.min_trades_for_analysis:
        log("info", f"Log analyzer skipped — {trade_count}/{env.min_trades_for_analysis} trades needed")
        return None

    log("info", f"Running Claude analysis ({trade_count} closed trades)...")

    prompt = _build_prompt(config, strategy_selector=strategy_selector)
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
    except (json.JSONDecodeError, ValueError) as err:
        log("error", f"Log analyzer: failed to parse Claude response as JSON — {err}",
            data={"preview": raw_text[:300]})
        return None

    try:
        analysis = Analysis(**parsed)
    except Exception as err:
        log("error", f"Log analyzer: Claude response failed validation — {err}")
        return None

    # Adversarial debate: challenge proposed changes
    rejected_params = _adversarial_review(analysis)
    if rejected_params:
        analysis.parameterChanges = [
            c for c in analysis.parameterChanges
            if c.parameter not in rejected_params
        ]

    # Ensemble verification: run 2 additional quick analyses with Sonnet
    # Only keep parameter changes that appear in 2+ of the 3 total analyses
    if analysis.parameterChanges and env.anthropic_api_key:
        analysis.parameterChanges = _ensemble_verify(
            prompt, analysis.parameterChanges, client
        )

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

    # Auto-create GitHub issues for new strategy suggestions
    for suggestion in analysis.newStrategySuggestions:
        create_data_gap_issue(
            suggestion=suggestion,
            context=f"Claude analysis (health={analysis.overallHealthScore}/100): {analysis.summary[:200]}",
        )

    # Auto-create GitHub issues for data source suggestions
    for ds in analysis.dataSourceSuggestions:
        create_data_gap_issue(
            suggestion=f"[Data Source] {ds.source}: {ds.rationale}",
            context=f"Priority: {ds.priority}. Claude analysis (health={analysis.overallHealthScore}/100): {analysis.summary[:200]}",
        )

    # Visual chart analysis for top traded symbols
    trades = get_closed_trades(50)
    chart_symbols = list(dict.fromkeys(t.symbol for t in trades))[:3]  # top 3 most recent
    for sym in chart_symbols:
        try:
            chart_png = render_chart(sym)
            if chart_png:
                visual = analyze_chart(sym, chart_png, context=analysis.summary[:200])
                if visual:
                    log("info", f"Chart analysis for {sym}: {visual[:300]}", symbol=sym)
        except Exception as err:
            log("warn", f"Chart analysis skipped for {sym}: {err}", symbol=sym)

    # Record analysis in memory for future context
    memory = get_analysis_memory()
    insights = analysis.topIssues + [si.observation for si in analysis.strategyInsights]
    memory.record_analysis(analysis.summary, insights)
    memory.decay_and_prune()

    return analysis
