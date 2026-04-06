/**
 * Standalone Claude log analysis script.
 *
 * Run this manually to trigger an immediate analysis:
 *   tsx scripts/analyze-logs.ts
 *
 * Or let the main process call it on an interval.
 * This script can also be invoked by Claude Code hooks for automated self-improvement.
 */

import { runAnalysis } from '../src/self-healing/log-analyzer.js';
import { defaultScannerConfig } from '../src/config.js';

const config = { ...defaultScannerConfig };

console.log('Running Claude log analysis...\n');

runAnalysis(config)
  .then(result => {
    if (!result) {
      console.log('Analysis skipped (see logs for reason)');
      process.exit(0);
    }

    console.log('\n═══ ANALYSIS COMPLETE ═══\n');
    console.log('Summary:', result.summary);
    console.log('\nTop Issues:');
    result.topIssues.forEach((issue, i) => console.log(`  ${i + 1}. ${issue}`));

    console.log('\nStrategy Insights:');
    result.strategyInsights.forEach(si => {
      console.log(`  [${si.strategy}] ${si.observation}`);
      console.log(`    → ${si.recommendation}`);
    });

    if (result.newStrategySuggestions.length > 0) {
      console.log('\nNew Strategy Suggestions from Claude:');
      result.newStrategySuggestions.forEach((s, i) => console.log(`  ${i + 1}. ${s}`));
    }

    console.log(`\nConfidence: ${result.confidenceLevel}`);
    console.log('\nParameter changes applied (see logs for details)');
  })
  .catch((err: unknown) => {
    console.error('Analysis failed:', err);
    process.exit(1);
  });
