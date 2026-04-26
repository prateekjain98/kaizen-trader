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

/**
 * Delete closed positions older than 90 days. Recent trades stay queryable;
 * historical analytics should already be aggregated into metrics by then.
 * Uses by_paperTrading_and_closed_at when filtering, here scans full table
 * because we don't filter by paperTrading.
 */
export const cleanupOldClosedPositions = internalMutation({
  args: {},
  handler: async (ctx) => {
    const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("positions")
      .withIndex("by_closed_at", (q) => q.lt("closedAt", cutoff))
      .take(500);
    for (const p of old) {
      await ctx.db.delete(p._id);
    }
  },
});

/**
 * Delete trades older than 90 days. Trade rows are entry/exit records linked
 * to positions; once the position is gone, the trade row has no reader.
 */
export const cleanupOldTrades = internalMutation({
  args: {},
  handler: async (ctx) => {
    const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("trades")
      .withIndex("by_placedAt", (q) => q.lt("placedAt", cutoff))
      .take(500);
    for (const t of old) {
      await ctx.db.delete(t._id);
    }
  },
});

/**
 * Delete diagnoses older than 90 days. Self-healer learning history.
 * Recent ones drive parameter adaptation; older ones are pure storage cost.
 */
export const cleanupOldDiagnoses = internalMutation({
  args: {},
  handler: async (ctx) => {
    const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("diagnoses")
      .withIndex("by_timestamp", (q) => q.lt("timestamp", cutoff))
      .take(500);
    for (const d of old) {
      await ctx.db.delete(d._id);
    }
  },
});

/**
 * Delete journal entries older than 90 days.
 */
export const cleanupOldJournal = internalMutation({
  args: {},
  handler: async (ctx) => {
    const cutoff = Date.now() - 90 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("tradeJournal")
      .withIndex("by_timestamp", (q) => q.lt("timestamp", cutoff))
      .take(500);
    for (const j of old) {
      await ctx.db.delete(j._id);
    }
  },
});

/**
 * Delete config-history snapshots older than 30 days. Each healing event
 * snapshots the full config; at high healing frequencies this grows fast.
 */
export const cleanupOldConfigHistory = internalMutation({
  args: {},
  handler: async (ctx) => {
    const cutoff = Date.now() - 30 * 24 * 60 * 60 * 1000;
    const old = await ctx.db
      .query("scannerConfigHistory")
      .withIndex("by_timestamp", (q) => q.lt("timestamp", cutoff))
      .take(500);
    for (const c of old) {
      await ctx.db.delete(c._id);
    }
  },
});
