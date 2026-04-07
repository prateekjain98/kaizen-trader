"""Standalone Claude log analysis script."""

import sys
from dataclasses import asdict

sys.path.insert(0, ".")

from src.self_healing.log_analyzer import run_analysis
from src.config import default_scanner_config
from src.types import ScannerConfig

config = ScannerConfig(**asdict(default_scanner_config))

print("Running Claude log analysis...\n")

result = run_analysis(config)

if not result:
    print("Analysis skipped (see logs for reason)")
    sys.exit(0)

print("\n=== ANALYSIS COMPLETE ===\n")
print("Summary:", result.summary)
print("\nTop Issues:")
for i, issue in enumerate(result.topIssues, 1):
    print(f"  {i}. {issue}")

print("\nStrategy Insights:")
for si in result.strategyInsights:
    print(f"  [{si.strategy}] {si.observation}")
    print(f"    -> {si.recommendation}")

if result.newStrategySuggestions:
    print("\nNew Strategy Suggestions from Claude:")
    for i, s in enumerate(result.newStrategySuggestions, 1):
        print(f"  {i}. {s}")

print(f"\nHealth Score: {result.overallHealthScore}/100")
print("\nParameter changes applied (see logs for details)")
