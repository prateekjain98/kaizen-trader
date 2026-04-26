import { mutation, internalMutation } from "./_generated/server";
import { v } from "convex/values";

export const insertPosition = mutation({
  args: {
    positionId: v.string(),
    symbol: v.string(),
    productId: v.string(),
    strategy: v.string(),
    side: v.union(v.literal("long"), v.literal("short")),
    tier: v.string(),
    entryPrice: v.float64(),
    quantity: v.float64(),
    sizeUsd: v.float64(),
    openedAt: v.float64(),
    highWatermark: v.float64(),
    lowWatermark: v.float64(),
    currentPrice: v.float64(),
    trailPct: v.float64(),
    stopPrice: v.float64(),
    maxHoldMs: v.float64(),
    qualScore: v.float64(),
    signalId: v.string(),
    status: v.union(v.literal("open"), v.literal("closing"), v.literal("closed")),
    exitPrice: v.optional(v.float64()),
    closedAt: v.optional(v.float64()),
    pnlUsd: v.optional(v.float64()),
    pnlPct: v.optional(v.float64()),
    exitReason: v.optional(v.string()),
    paperTrading: v.boolean(),
    maePct: v.optional(v.float64()),
    mfePct: v.optional(v.float64()),
    partialExitPct: v.optional(v.float64()),
    trancheCount: v.optional(v.float64()),
    avgEntryPrice: v.optional(v.float64()),
    originalQuantity: v.optional(v.float64()),
    entrySizeUsd: v.optional(v.float64()),
    totalCommission: v.optional(v.float64()),
    initialStopPrice: v.optional(v.float64()),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("positions")
      .withIndex("by_positionId", (q) => q.eq("positionId", args.positionId))
      .first();
    if (existing) return;
    await ctx.db.insert("positions", args);
  },
});

export const updatePositionPrice = mutation({
  args: {
    positionId: v.string(),
    currentPrice: v.float64(),
    highWatermark: v.float64(),
    lowWatermark: v.float64(),
    stopPrice: v.float64(),
    quantity: v.optional(v.float64()),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("positions")
      .withIndex("by_positionId", (q) => q.eq("positionId", args.positionId))
      .first();
    if (!existing) return;
    const updates: Record<string, number> = {
      currentPrice: args.currentPrice,
      highWatermark: args.highWatermark,
      lowWatermark: args.lowWatermark,
      stopPrice: args.stopPrice,
    };
    if (args.quantity !== undefined) {
      updates.quantity = args.quantity;
    }
    await ctx.db.patch(existing._id, updates);
  },
});

export const updatePositionClose = mutation({
  args: {
    positionId: v.string(),
    exitPrice: v.float64(),
    pnlUsd: v.float64(),
    pnlPct: v.float64(),
    exitReason: v.string(),
    closedAt: v.float64(),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("positions")
      .withIndex("by_positionId", (q) => q.eq("positionId", args.positionId))
      .first();
    if (!existing) {
      throw new Error(`Position ${args.positionId} not found`);
    }
    await ctx.db.patch(existing._id, {
      status: "closed",
      exitPrice: args.exitPrice,
      closedAt: args.closedAt,
      pnlUsd: args.pnlUsd,
      pnlPct: args.pnlPct,
      exitReason: args.exitReason,
    });
  },
});

// Admin-only: locked behind internalMutation so it cannot be called over
// the public HTTP API. Run via Convex dashboard or scheduled jobs.
export const deletePositionById = internalMutation({
  args: { positionId: v.string() },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("positions")
      .withIndex("by_positionId", (q) => q.eq("positionId", args.positionId))
      .first();
    if (existing) {
      await ctx.db.delete(existing._id);
      return true;
    }
    return false;
  },
});

// Admin-only.
export const deduplicateOpenPositions = internalMutation({
  args: {},
  handler: async (ctx) => {
    const open = await ctx.db
      .query("positions")
      .withIndex("by_status", (q) => q.eq("status", "open"))
      .take(500);
    const seen = new Set<string>();
    let removed = 0;
    for (const pos of open) {
      if (seen.has(pos.positionId)) {
        await ctx.db.delete(pos._id);
        removed++;
      } else {
        seen.add(pos.positionId);
      }
    }
    return { removed, remaining: open.length - removed };
  },
});

export const insertTrade = mutation({
  args: {
    tradeId: v.string(),
    positionId: v.string(),
    side: v.union(v.literal("long"), v.literal("short"),
                  v.literal("buy"), v.literal("sell")),
    symbol: v.string(),
    quantity: v.float64(),
    sizeUsd: v.float64(),
    price: v.float64(),
    orderId: v.optional(v.string()),
    status: v.string(),
    error: v.optional(v.string()),
    paperTrading: v.boolean(),
    placedAt: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("trades", args);
  },
});

export const insertLog = mutation({
  args: {
    logId: v.string(),
    level: v.union(
      v.literal("info"), v.literal("warn"), v.literal("error"),
      v.literal("trade"), v.literal("debug"), v.literal("critical"),
    ),
    message: v.string(),
    symbol: v.optional(v.string()),
    strategy: v.optional(v.string()),
    data: v.optional(v.string()),
    ts: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("logs", args);
  },
});

// Batched log insert — collapses up to ~500 log writes into a single function
// call, drastically cutting Convex function-call cost. The Python client
// queues logs and ships them via this mutation in chunks.
export const insertLogsBatch = mutation({
  args: {
    logs: v.array(v.object({
      logId: v.string(),
      level: v.union(
        v.literal("info"), v.literal("warn"), v.literal("error"),
        v.literal("trade"), v.literal("debug"), v.literal("critical"),
      ),
      message: v.string(),
      symbol: v.optional(v.string()),
      strategy: v.optional(v.string()),
      data: v.optional(v.string()),
      ts: v.float64(),
    })),
  },
  handler: async (ctx, args) => {
    for (const log of args.logs) {
      await ctx.db.insert("logs", log);
    }
  },
});

export const insertDiagnosis = mutation({
  args: {
    positionId: v.string(),
    symbol: v.string(),
    strategy: v.string(),
    pnlPct: v.float64(),
    holdMs: v.float64(),
    exitReason: v.string(),
    lossReason: v.string(),
    entryQualScore: v.float64(),
    marketPhaseAtEntry: v.string(),
    action: v.string(),
    parameterChanges: v.string(),
    timestamp: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("diagnoses", args);
  },
});

export const snapshotConfig = mutation({
  args: {
    config: v.string(),
    reason: v.string(),
    timestamp: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("scannerConfigHistory", args);
  },
});

export const insertParameterDelta = mutation({
  args: {
    parameter: v.string(),
    oldValue: v.float64(),
    newValue: v.float64(),
    reason: v.string(),
    source: v.string(),
    tradesBeforeSnapshot: v.string(),
    tradesAfterSnapshot: v.optional(v.string()),
    evaluationStatus: v.string(),
    evaluationTimestamp: v.optional(v.float64()),
    verdict: v.optional(v.string()),
    timestamp: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("parameterDeltas", args);
  },
});

export const updateParameterDelta = mutation({
  args: {
    id: v.id("parameterDeltas"),
    tradesAfterSnapshot: v.optional(v.string()),
    evaluationStatus: v.optional(v.string()),
    evaluationTimestamp: v.optional(v.float64()),
    verdict: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const { id, ...updates } = args;
    const cleanUpdates: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(updates)) {
      if (value !== undefined) {
        cleanUpdates[key] = value;
      }
    }
    await ctx.db.patch(id, cleanUpdates);
  },
});

export const insertGithubIssue = mutation({
  args: {
    issueNumber: v.float64(),
    title: v.string(),
    body: v.string(),
    triggerType: v.string(),
    triggerData: v.string(),
    createdAt: v.float64(),
    status: v.string(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("githubIssues", args);
  },
});

export const insertTradeJournal = mutation({
  args: {
    positionId: v.string(),
    symbol: v.string(),
    strategy: v.string(),
    rMultiple: v.optional(v.float64()),
    holdHours: v.optional(v.float64()),
    maePct: v.optional(v.float64()),
    mfePct: v.optional(v.float64()),
    partialExitPct: v.optional(v.float64()),
    exitReason: v.optional(v.string()),
    pnlPct: v.optional(v.float64()),
    regimeAtEntry: v.optional(v.string()),
    regimeAtExit: v.optional(v.string()),
    wasPartialBeneficial: v.optional(v.float64()),
    timestamp: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("tradeJournal", args);
  },
});

// Admin-only — wipes the entire trading database. Convert kept here for
// emergency use only via the Convex dashboard.
export const clearAllData = internalMutation({
  args: {},
  handler: async (ctx) => {
    const tables = [
      "positions",
      "trades",
      "logs",
      "diagnoses",
      "scannerConfigHistory",
      "parameterDeltas",
      "githubIssues",
      "tradeJournal",
      "metrics",
    ] as const;
    // Loop until table is empty (or we hit the Convex per-mutation read cap).
    // Previous .take(1000) silently left partial state if any table had >1000
    // rows. The 8000-read soft cap means each invocation can clear up to ~8k
    // rows total across all tables; caller should re-invoke until counts are 0.
    const counts: Record<string, number> = {};
    let totalReads = 0;
    const READ_BUDGET = 7000;  // leave headroom for Convex internal accounting
    for (const table of tables) {
      counts[table] = 0;
      while (totalReads < READ_BUDGET) {
        const rows = await ctx.db.query(table).take(500);
        if (rows.length === 0) break;
        for (const row of rows) {
          await ctx.db.delete(row._id);
        }
        counts[table] += rows.length;
        totalReads += rows.length;
      }
    }
    return counts;
  },
});

export const closeOrphanedPositions = mutation({
  args: {
    exitReason: v.string(),
    closedAt: v.float64(),
  },
  handler: async (ctx, args) => {
    // Bounded: Convex caps mutations at ~8k document reads / 1MB writes.
    // If there's a backlog of orphans, the caller (Python) re-invokes until
    // `closed === 0`. Without this cap a runaway watchdog state could OOM
    // the mutation and silently leave orphans.
    const BATCH = 200;
    const open = await ctx.db
      .query("positions")
      .withIndex("by_status", (q) => q.eq("status", "open"))
      .take(BATCH);
    let closed = 0;
    for (const pos of open) {
      // Do NOT write pnlUsd/pnlPct: these positions were orphaned (we don't
      // know the actual exit price). Writing 0 polluted win-rate / Sharpe /
      // metrics queries by counting them as breakeven. Leave fields undefined
      // and require analytics queries to filter exit_reason="orphaned_restart"
      // (or any future synthetic close reason).
      await ctx.db.patch(pos._id, {
        status: "closed",
        exitReason: args.exitReason,
        closedAt: args.closedAt,
      });
      closed++;
    }
    return { closed, positionIds: open.map((p) => p.positionId) };
  },
});

export const insertMetrics = mutation({
  args: {
    windowStartMs: v.float64(),
    windowEndMs: v.float64(),
    errorCount: v.float64(),
    warnCount: v.float64(),
    tradeCount: v.float64(),
    healingCount: v.float64(),
    avgPnlPct: v.optional(v.float64()),
    winRate: v.optional(v.float64()),
    computedAt: v.float64(),
  },
  handler: async (ctx, args) => {
    await ctx.db.insert("metrics", args);
  },
});

// Admin-only.
export const deleteByExitReason = internalMutation({
  args: { exitReason: v.string() },
  handler: async (ctx, args) => {
    const positions = await ctx.db.query("positions").take(500);
    let deleted = 0;
    for (const pos of positions) {
      if (pos.exitReason === args.exitReason) {
        await ctx.db.delete(pos._id);
        deleted++;
      }
    }
    return { deleted };
  },
});

// Admin-only.
export const deleteClosedPositions = internalMutation({
  args: {},
  handler: async (ctx) => {
    const closed = await ctx.db
      .query("positions")
      .withIndex("by_status", (q) => q.eq("status", "closed"))
      .collect();
    let deleted = 0;
    for (const pos of closed) {
      await ctx.db.delete(pos._id);
      deleted++;
    }
    return { deleted };
  },
});
