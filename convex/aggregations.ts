import { internalMutation } from "./_generated/server";

/**
 * Compute aggregated metrics over the last hour for the dashboard health panel.
 * Called by the cron job every 15 minutes.
 */
export const computeMetrics = internalMutation({
  args: {},
  handler: async (ctx) => {
    const now = Date.now();
    const oneHourAgo = now - 60 * 60 * 1000;

    // Count errors and warnings in the last hour
    const recentLogs = await ctx.db
      .query("logs")
      .withIndex("by_ts", (q) => q.gte("ts", oneHourAgo))
      .collect();

    let errorCount = 0;
    let warnCount = 0;
    for (const log of recentLogs) {
      if (log.level === "error") errorCount++;
      if (log.level === "warn") warnCount++;
    }

    // Count trades closed in the last hour
    const recentClosed = await ctx.db
      .query("positions")
      .withIndex("by_closed_at", (q) => q.gte("closedAt", oneHourAgo))
      .collect();

    const tradeCount = recentClosed.length;
    let totalPnlPct = 0;
    let wins = 0;
    for (const pos of recentClosed) {
      if (pos.pnlPct !== undefined) {
        totalPnlPct += pos.pnlPct;
        if (pos.pnlPct > 0) wins++;
      }
    }

    const avgPnlPct = tradeCount > 0 ? totalPnlPct / tradeCount : undefined;
    const winRate = tradeCount > 0 ? (wins / tradeCount) * 100 : undefined;

    // Count healing actions (diagnoses) in the last hour
    const recentDiagnoses = await ctx.db
      .query("diagnoses")
      .withIndex("by_timestamp", (q) => q.gte("timestamp", oneHourAgo))
      .collect();

    const healingCount = recentDiagnoses.length;

    await ctx.db.insert("metrics", {
      windowStartMs: oneHourAgo,
      windowEndMs: now,
      errorCount,
      warnCount,
      tradeCount,
      healingCount,
      avgPnlPct,
      winRate,
      computedAt: now,
    });
  },
});
