import { query } from "./_generated/server";
import { v } from "convex/values";

export const getOpenPositions = query({
  args: { paperTrading: v.optional(v.boolean()) },
  handler: async (ctx, args) => {
    let open = await ctx.db
      .query("positions")
      .withIndex("by_status", (q) => q.eq("status", "open"))
      .collect();
    let closing = await ctx.db
      .query("positions")
      .withIndex("by_status", (q) => q.eq("status", "closing"))
      .collect();
    if (args.paperTrading !== undefined) {
      open = open.filter((p) => p.paperTrading === args.paperTrading);
      closing = closing.filter((p) => p.paperTrading === args.paperTrading);
    }
    return [...open, ...closing];
  },
});

export const getClosedTrades = query({
  args: {
    limit: v.optional(v.float64()),
    paperTrading: v.optional(v.boolean()),
  },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 200;
    let q = ctx.db
      .query("positions")
      .withIndex("by_closed_at")
      .order("desc")
      .filter((q) => q.eq(q.field("status"), "closed"));
    if (args.paperTrading !== undefined) {
      q = q.filter((f) => f.eq(f.field("paperTrading"), args.paperTrading!));
    }
    return await q.take(limit);
  },
});

export const getRecentLogs = query({
  args: {
    limit: v.optional(v.float64()),
    level: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 500;
    if (args.level) {
      return await ctx.db
        .query("logs")
        .withIndex("by_level_ts", (q) => q.eq("level", args.level!))
        .order("desc")
        .take(limit);
    }
    return await ctx.db
      .query("logs")
      .withIndex("by_ts")
      .order("desc")
      .take(limit);
  },
});

export const getRecentDiagnoses = query({
  args: { limit: v.optional(v.float64()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 50;
    return await ctx.db
      .query("diagnoses")
      .withIndex("by_timestamp")
      .order("desc")
      .take(limit);
  },
});

export const getConfigHistory = query({
  args: { limit: v.optional(v.float64()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 20;
    return await ctx.db
      .query("scannerConfigHistory")
      .withIndex("by_timestamp")
      .order("desc")
      .take(limit);
  },
});

export const getPendingDeltas = query({
  args: {},
  handler: async (ctx) => {
    return await ctx.db
      .query("parameterDeltas")
      .withIndex("by_status", (q) => q.eq("evaluationStatus", "pending"))
      .collect();
  },
});

export const getOpenIssues = query({
  args: {},
  handler: async (ctx) => {
    return await ctx.db
      .query("githubIssues")
      .withIndex("by_status", (q) => q.eq("status", "open"))
      .collect();
  },
});

export const getLatestMetrics = query({
  args: { limit: v.optional(v.float64()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 10;
    return await ctx.db
      .query("metrics")
      .withIndex("by_computed_at")
      .order("desc")
      .take(limit);
  },
});

export const getTradeJournal = query({
  args: { limit: v.optional(v.float64()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 50;
    return await ctx.db
      .query("tradeJournal")
      .withIndex("by_timestamp")
      .order("desc")
      .take(limit);
  },
});

export const getTradesByPosition = query({
  args: { positionId: v.string() },
  handler: async (ctx, args) => {
    return await ctx.db
      .query("trades")
      .withIndex("by_position", (q) => q.eq("positionId", args.positionId))
      .collect();
  },
});

export const getWinRateByStrategy = query({
  args: { limit: v.optional(v.float64()) },
  handler: async (ctx, args) => {
    const limit = args.limit ?? 500;
    const closed = await ctx.db
      .query("positions")
      .withIndex("by_closed_at")
      .order("desc")
      .filter((q) => q.eq(q.field("status"), "closed"))
      .take(limit);

    const byStrategy: Record<
      string,
      { total: number; wins: number; totalPnl: number }
    > = {};

    for (const pos of closed) {
      if (!byStrategy[pos.strategy]) {
        byStrategy[pos.strategy] = { total: 0, wins: 0, totalPnl: 0 };
      }
      const stats = byStrategy[pos.strategy];
      stats.total += 1;
      if (pos.pnlPct !== undefined && pos.pnlPct > 0) {
        stats.wins += 1;
      }
      stats.totalPnl += pos.pnlUsd ?? 0;
    }

    return Object.entries(byStrategy).map(([strategy, stats]) => ({
      strategy,
      total: stats.total,
      wins: stats.wins,
      winRate: stats.total > 0 ? (stats.wins / stats.total) * 100 : 0,
      totalPnlUsd: stats.totalPnl,
    }));
  },
});
