import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  positions: defineTable({
    positionId: v.string(), // maps to Position.id
    symbol: v.string(),
    productId: v.string(),
    strategy: v.string(),
    side: v.string(),
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
    status: v.string(), // "open" | "closing" | "closed"
    exitPrice: v.optional(v.float64()),
    closedAt: v.optional(v.float64()),
    pnlUsd: v.optional(v.float64()),
    pnlPct: v.optional(v.float64()),
    exitReason: v.optional(v.string()),
    paperTrading: v.boolean(),
  })
    .index("by_status", ["status"])
    .index("by_positionId", ["positionId"])
    .index("by_symbol", ["symbol"])
    .index("by_closed_at", ["closedAt"]),

  trades: defineTable({
    tradeId: v.string(),
    positionId: v.string(),
    side: v.string(),
    symbol: v.string(),
    quantity: v.float64(),
    sizeUsd: v.float64(),
    price: v.float64(),
    orderId: v.optional(v.string()),
    status: v.string(),
    error: v.optional(v.string()),
    paperTrading: v.boolean(),
    placedAt: v.float64(),
  }).index("by_position", ["positionId"]),

  logs: defineTable({
    logId: v.string(),
    level: v.string(),
    message: v.string(),
    symbol: v.optional(v.string()),
    strategy: v.optional(v.string()),
    data: v.optional(v.string()), // JSON string
    ts: v.float64(),
  })
    .index("by_ts", ["ts"])
    .index("by_level_ts", ["level", "ts"]),

  diagnoses: defineTable({
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
    parameterChanges: v.string(), // JSON string
    timestamp: v.float64(),
  }).index("by_timestamp", ["timestamp"]),

  scannerConfigHistory: defineTable({
    config: v.string(), // JSON string
    reason: v.string(),
    timestamp: v.float64(),
  }).index("by_timestamp", ["timestamp"]),

  parameterDeltas: defineTable({
    parameter: v.string(),
    oldValue: v.float64(),
    newValue: v.float64(),
    reason: v.string(),
    source: v.string(), // "immediate_healer" | "claude_analysis"
    tradesBeforeSnapshot: v.string(), // JSON: {win_rate, avg_pnl, count}
    tradesAfterSnapshot: v.optional(v.string()),
    evaluationStatus: v.string(), // "pending" | "evaluated" | "reverted"
    evaluationTimestamp: v.optional(v.float64()),
    verdict: v.optional(v.string()), // "improved" | "worsened" | "neutral"
    timestamp: v.float64(),
  })
    .index("by_status", ["evaluationStatus"])
    .index("by_timestamp", ["timestamp"]),

  githubIssues: defineTable({
    issueNumber: v.float64(),
    title: v.string(),
    body: v.string(),
    triggerType: v.string(), // "blind_spot" | "data_gap" | "chronic_underperformer"
    triggerData: v.string(),
    createdAt: v.float64(),
    status: v.string(), // "open" | "closed"
  })
    .index("by_trigger", ["triggerType", "triggerData"])
    .index("by_status", ["status"]),

  tradeJournal: defineTable({
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
  })
    .index("by_timestamp", ["timestamp"])
    .index("by_strategy", ["strategy"]),

  // Aggregated metrics computed by cron for dashboard health panel
  metrics: defineTable({
    windowStartMs: v.float64(),
    windowEndMs: v.float64(),
    errorCount: v.float64(),
    warnCount: v.float64(),
    tradeCount: v.float64(),
    healingCount: v.float64(),
    avgPnlPct: v.optional(v.float64()),
    winRate: v.optional(v.float64()),
    computedAt: v.float64(),
  }).index("by_computed_at", ["computedAt"]),
});
