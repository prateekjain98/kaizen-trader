import { internalMutation, internalQuery } from "./_generated/server";

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

/**
 * Delete logs older than 7 days to prevent unbounded storage growth.
 * Called by the cron job every 6 hours.
 */
export const cleanupOldLogs = internalMutation({
  args: {},
  handler: async (ctx) => {
    const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const oldLogs = await ctx.db
      .query("logs")
      .withIndex("by_ts", (q) => q.lt("ts", sevenDaysAgo))
      .take(500); // batch to stay within write limits

    for (const log of oldLogs) {
      await ctx.db.delete(log._id);
    }
  },
});

/**
 * Delete old metrics (older than 30 days) to prevent storage growth.
 */
export const cleanupOldMetrics = internalMutation({
  args: {},
  handler: async (ctx) => {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("metrics")
      .withIndex("by_computed_at", (q) => q.lt("computedAt", thirtyDaysAgo))
      .take(100);

    for (const m of old) {
      await ctx.db.delete(m._id);
    }
  },
});
