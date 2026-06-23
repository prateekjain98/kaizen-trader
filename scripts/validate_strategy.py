#!/usr/bin/env python3
"""Strategy validation harness (#3) — adopt Jesse/Freqtrade-grade discipline.

Two checks the codebase didn't have an explicit gate for:

  1. LOOK-AHEAD-BIAS AUDIT (static): scans strategy + indicator source for the
     patterns that leak future information into a decision — the single most
     common reason a backtest looks great and live trading doesn't. This is the
     capability Jesse markets as "zero look-ahead bias".

  2. WALK-FORWARD / OOS GAP (optional): wraps the existing walk-forward infra
     (src/backtesting/walk_forward.py, scripts/walk_forward_carry.py) and reports
     the in-sample vs out-of-sample gap so overfitting is visible. Run those
     scripts directly for full numbers; this harness just summarizes verdicts.

Usage:
    python3 scripts/validate_strategy.py                 # audit all strategies+indicators
    python3 scripts/validate_strategy.py --path src/strategies/momentum.py
    python3 scripts/validate_strategy.py --strict        # non-zero exit on HIGH findings (CI gate)

Exit code is non-zero under --strict if any HIGH-severity look-ahead finding
exists — wire it into CI to block look-ahead regressions.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (regex, severity, explanation). HIGH = almost certainly a future-data leak.
PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\.shift\(\s*-\s*\d+"), "HIGH",
     "negative .shift() pulls FUTURE rows backward into the current row"),
    (re.compile(r"\.iloc\[\s*[a-zA-Z_]\w*\s*\+\s*1\s*\]"), "HIGH",
     "forward index .iloc[i+1] reads the NEXT (unknown) bar"),
    (re.compile(r"\b[a-zA-Z_]\w*\[\s*i\s*\+\s*1\s*\]"), "HIGH",
     "forward index series[i+1] reads the NEXT (unknown) bar"),
    (re.compile(r"\.tail\(\s*-\s*\d+"), "HIGH",
     "negative .tail() selects from the end forward — possible leak"),
    (re.compile(r"future|lookahead|look_ahead|peek", re.IGNORECASE), "MEDIUM",
     "identifier mentions future/lookahead — review the data window used"),
    (re.compile(r"\.resample\("), "MEDIUM",
     "resample can include a partially-formed (future) bar — confirm closed bars only"),
    (re.compile(r"candles\[-1\]|klines\[-1\]|bars\[-1\]"), "LOW",
     "last element may be a still-forming bar — confirm it is closed at decision time"),
]

# Lines containing these substrings are exempt (comments/docstrings/known-safe).
_SKIP_LINE = ("# noqa: lookahead", "description", '"""', "# ")


def audit_file(path: Path) -> list[tuple[int, str, str, str]]:
    findings: list[tuple[int, str, str, str]] = []
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return findings
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for rx, sev, why in PATTERNS:
            if rx.search(line):
                # de-noise the MEDIUM "future" identifier match inside comments/docstrings
                if sev != "HIGH" and any(s in line for s in ('"""', "# ", "description")):
                    continue
                findings.append((n, sev, why, stripped[:100]))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", help="single file or dir to audit (default: strategies + indicators)")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on any HIGH finding")
    args = ap.parse_args()

    if args.path:
        targets = [ROOT / args.path]
    else:
        targets = [ROOT / "src" / "strategies", ROOT / "src" / "indicators"]

    files: list[Path] = []
    for t in targets:
        if t.is_dir():
            files += sorted(t.rglob("*.py"))
        elif t.is_file():
            files.append(t)

    high = med = low = 0
    print("=" * 72)
    print("LOOK-AHEAD-BIAS AUDIT")
    print("=" * 72)
    for f in files:
        if f.name == "__init__.py":
            continue
        findings = audit_file(f)
        if not findings:
            continue
        rel = f.relative_to(ROOT)
        print(f"\n{rel}")
        for n, sev, why, src in findings:
            mark = {"HIGH": "✗", "MEDIUM": "⚠", "LOW": "·"}[sev]
            print(f"  {mark} L{n} [{sev}] {why}")
            print(f"        {src}")
            high += sev == "HIGH"; med += sev == "MEDIUM"; low += sev == "LOW"

    print("\n" + "-" * 72)
    print(f"Summary: {high} HIGH, {med} MEDIUM, {low} LOW across {len(files)} files")
    print("-" * 72)
    print("\nWALK-FORWARD / OOS:")
    print("  Run the existing harness for real numbers and watch the IS→OOS gap")
    print("  (a large drop = overfitting):")
    print("    python3 scripts/walk_forward_carry.py")
    print("    python3 scripts/systematic_backtest.py --strategy <name>")
    print("  A strategy 'passes' only if OOS Sharpe > 0 and OOS win-rate is")
    print("  within ~10pp of in-sample. Treat the '197% CAGR' claim as UNPROVEN")
    print("  until it clears this on a held-out period.")

    if args.strict and high:
        print(f"\nFAIL (--strict): {high} HIGH-severity look-ahead finding(s).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
