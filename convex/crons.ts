import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

// Aggregate metrics every 15 minutes for dashboard health panel
crons.interval(
  "aggregate_metrics",
  { minutes: 15 },
  internal.aggregations.computeMetrics
);

export default crons;
