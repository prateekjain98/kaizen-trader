/**
 * Performance report — prints a full metrics breakdown to stdout.
 *
 * Usage:
 *   tsx scripts/performance.ts
 *   tsx scripts/performance.ts --last 50    # last 50 trades only
 *   tsx scripts/performance.ts --csv        # output CSV for spreadsheet import
 */

import { computeMetrics, formatMetrics } from '../src/evaluation/metrics.js';
import { getClosedTrades } from '../src/storage/database.js';

const args = process.argv.slice(2);
const lastIndex = args.indexOf('--last');
const limit = lastIndex >= 0 ? parseInt(args[lastIndex + 1] ?? '500') : 500;
const csvMode = args.includes('--csv');

const metrics = computeMetrics(limit);
const trades  = getClosedTrades(limit);

if (csvMode) {
  console.log('symbol,strategy,side,tier,pnl_pct,pnl_usd,hold_hours,exit_reason,qual_score,opened_at');
  for (const t of trades) {
    const holdH = t.closedAt ? ((t.closedAt - t.openedAt) / 3_600_000).toFixed(2) : '';
    console.log([
      t.symbol, t.strategy, t.side, t.tier,
      t.pnlPct?.toFixed(4) ?? '', t.pnlUsd?.toFixed(2) ?? '',
      holdH, t.exitReason ?? '', t.qualScore,
      t.openedAt ? new Date(t.openedAt).toISOString() : '',
    ].join(','));
  }
} else {
  console.log('\n═══════════════════════════════════════════════════');
  console.log('  kaizen-trader — Performance Report');
  console.log(`  ${new Date().toISOString()}`);
  console.log('═══════════════════════════════════════════════════\n');
  console.log(formatMetrics(metrics));
  console.log('\n═══════════════════════════════════════════════════\n');
}
