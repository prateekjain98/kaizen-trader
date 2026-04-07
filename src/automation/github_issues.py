"""Auto-create GitHub issues for blind spots, data gaps, and chronic underperformers."""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.storage.database import log


@dataclass
class IssueRecord:
    trigger_type: str  # "blind_spot" | "data_gap" | "chronic_underperformer"
    trigger_key: str   # dedup key
    issue_number: Optional[int] = None
    created_at: float = 0


# In-memory dedup registry + daily cap
_created_issues: dict[str, IssueRecord] = {}
_daily_count: int = 0
_daily_date: str = ""
_lock = threading.Lock()
MAX_ISSUES_PER_DAY = 3


def _get_repo() -> str:
    """Read GITHUB_REPO from env at call time (not import time)."""
    return os.environ.get("GITHUB_REPO", "")


def _reset_daily_cap() -> None:
    """Reset the daily issue counter if the date has changed."""
    global _daily_count, _daily_date
    today = time.strftime("%Y-%m-%d")
    with _lock:
        if today != _daily_date:
            _daily_date = today
            _daily_count = 0


def _is_duplicate(trigger_type: str, trigger_key: str) -> bool:
    """Check if an issue with this trigger already exists."""
    with _lock:
        return trigger_key in _created_issues


def _create_issue_via_gh(title: str, body: str, labels: list[str]) -> Optional[int]:
    """Create a GitHub issue using the `gh` CLI. Returns issue number or None."""
    repo = _get_repo()
    if not repo:
        log("warn", "GITHUB_REPO not set -- skipping issue creation")
        return None

    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for label in labels:
        cmd.extend(["--label", label])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # Parse issue URL to get number
            url = result.stdout.strip()
            issue_number = int(url.rstrip("/").split("/")[-1])
            log("info", f"GitHub issue #{issue_number} created: {title}")
            return issue_number
        else:
            log("error", f"gh issue create failed: {result.stderr[:200]}")
            return None
    except Exception as e:
        log("error", f"Failed to create GitHub issue: {e}")
        return None


def create_blind_spot_issue(fingerprint_key: str, occurrences: int,
                            avg_loss_pct: float, affected_strategies: list[str]) -> Optional[int]:
    """Create an issue for a detected blind spot pattern."""
    global _daily_count
    _reset_daily_cap()

    trigger_key = f"blind_spot:{fingerprint_key}"
    if _is_duplicate("blind_spot", trigger_key):
        return None

    with _lock:
        if _daily_count >= MAX_ISSUES_PER_DAY:
            log("warn", "Daily GitHub issue cap reached -- skipping blind spot issue")
            return None

    title = f"Blind Spot: {fingerprint_key}"
    body = f"""## Detected Blind Spot Pattern

**Fingerprint:** `{fingerprint_key}`
**Occurrences:** {occurrences}
**Average Loss:** {avg_loss_pct:.2f}%
**Affected Strategies:** {', '.join(affected_strategies)}

### What this means
The self-healer has detected a recurring loss pattern that it cannot classify into any known loss reason. This pattern has appeared {occurrences} times, suggesting a systematic issue.

### Suggested Investigation
1. Check recent trades matching this pattern in the diagnoses table
2. Look for common market conditions when these losses occur
3. Consider adding a new loss reason classification for this pattern
4. Check if a new data source or signal could help predict this pattern

### Auto-generated
This issue was created automatically by the Kaizen Trader blind spot detector.
"""

    issue_num = _create_issue_via_gh(title, body, ["blind-spot", "automated"])
    if issue_num:
        with _lock:
            _created_issues[trigger_key] = IssueRecord(
                trigger_type="blind_spot", trigger_key=trigger_key,
                issue_number=issue_num, created_at=time.time()
            )
            _daily_count += 1
    return issue_num


def create_data_gap_issue(suggestion: str, context: str = "") -> Optional[int]:
    """Create an issue when Claude analysis suggests a missing data source."""
    global _daily_count
    _reset_daily_cap()

    # Use first 80 chars as dedup key
    trigger_key = f"data_gap:{suggestion[:80]}"
    if _is_duplicate("data_gap", trigger_key):
        return None

    with _lock:
        if _daily_count >= MAX_ISSUES_PER_DAY:
            return None

    title = f"Data Gap: {suggestion[:60]}"
    body = f"""## Missing Data Source / Integration

**Suggestion:** {suggestion}

### Context
{context or 'Identified during periodic Claude log analysis.'}

### Why this matters
The AI analysis loop identified that this data source could improve trading decisions. Losses may be occurring because the trader lacks access to this information.

### Action Items
- [ ] Evaluate the suggested data source
- [ ] Check API availability and costs
- [ ] Implement integration if viable
- [ ] Add to qualification scorer weights

### Auto-generated
This issue was created automatically by the Kaizen Trader Claude analysis loop.
"""

    issue_num = _create_issue_via_gh(title, body, ["data-gap", "automated"])
    if issue_num:
        with _lock:
            _created_issues[trigger_key] = IssueRecord(
                trigger_type="data_gap", trigger_key=trigger_key,
                issue_number=issue_num, created_at=time.time()
            )
            _daily_count += 1
    return issue_num


def create_chronic_underperformer_issue(strategy_id: str, days_disabled: int,
                                        win_rate: float, sharpe: float,
                                        consecutive_losses: int) -> Optional[int]:
    """Create an issue when a strategy has been disabled for >14 days."""
    global _daily_count
    _reset_daily_cap()

    trigger_key = f"chronic:{strategy_id}"
    if _is_duplicate("chronic_underperformer", trigger_key):
        return None

    with _lock:
        if _daily_count >= MAX_ISSUES_PER_DAY:
            return None

    title = f"Chronic Underperformer: {strategy_id}"
    body = f"""## Strategy Disabled for {days_disabled} Days

**Strategy:** `{strategy_id}`
**Days Disabled:** {days_disabled}
**Win Rate:** {win_rate:.1f}%
**Sharpe Ratio:** {sharpe:.2f}
**Consecutive Losses at Disable:** {consecutive_losses}

### What this means
This strategy has been disabled by the Darwinian selection system for over 14 days with no sign of recovery. It may need fundamental changes or removal.

### Suggested Actions
- [ ] Review the strategy logic for systematic flaws
- [ ] Check if market conditions have permanently shifted
- [ ] Consider adding new signals or filters
- [ ] Backtest proposed changes before re-enabling
- [ ] Remove the strategy if it cannot be fixed

### Auto-generated
This issue was created automatically by the Kaizen Trader Darwinian strategy selector.
"""

    issue_num = _create_issue_via_gh(title, body, ["chronic-underperformer", "automated"])
    if issue_num:
        with _lock:
            _created_issues[trigger_key] = IssueRecord(
                trigger_type="chronic_underperformer", trigger_key=trigger_key,
                issue_number=issue_num, created_at=time.time()
            )
            _daily_count += 1
    return issue_num


def reset_state() -> None:
    """Reset all module-level state. Used in tests."""
    global _created_issues, _daily_count, _daily_date
    with _lock:
        _created_issues.clear()
        _daily_count = 0
        _daily_date = ""
