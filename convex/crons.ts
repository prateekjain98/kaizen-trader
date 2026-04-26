import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

// Aggregate metrics every 15 minutes for dashboard health panel
crons.interval(
  "aggregate_metrics",
  { minutes: 15 },
  internal.aggregations.computeMetrics
);

// Clean up logs older than 7 days every 6 hours
crons.interval(
  "cleanup_old_logs",
  { hours: 6 },
  internal.aggregations.cleanupOldLogs
);

// Clean up metrics older than 30 days every 24 hours
crons.interval(
  "cleanup_old_metrics",
  { hours: 24 },
  internal.aggregations.cleanupOldMetrics
);

// Daily cleanups for the previously-unbounded tables. Each cleanup is bounded
// to 500 rows per call; if a backlog exists, repeated cron fires drain it
// gradually instead of OOMing the mutation.
crons.interval(
  "cleanup_closed_positions",
  { hours: 24 },
  internal.aggregations.cleanupOldClosedPositions
);
crons.interval(
  "cleanup_old_trades",
  { hours: 24 },
  internal.aggregations.cleanupOldTrades
);
crons.interval(
  "cleanup_old_diagnoses",
  { hours: 24 },
  internal.aggregations.cleanupOldDiagnoses
);
crons.interval(
  "cleanup_old_journal",
  { hours: 24 },
  internal.aggregations.cleanupOldJournal
);
crons.interval(
  "cleanup_old_config_history",
  { hours: 24 },
  internal.aggregations.cleanupOldConfigHistory
);

export default crons;
