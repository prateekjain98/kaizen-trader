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

export default crons;
