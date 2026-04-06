/**
 * Strategy registry — maps strategy IDs to their scanner functions.
 * Each strategy takes market context and returns a TradeSignal or null.
 */

export { scanMomentum } from './momentum.js';
export { scanMeanReversion } from './mean-reversion.js';
export { scanListingPump } from './listing-pump.js';
export { scanWhaleAccumulation } from './whale-tracker.js';
export { scanFundingExtreme } from './funding-extreme.js';
export { scanLiquidationCascade } from './liquidation-cascade.js';
export { scanOrderBookImbalance } from './orderbook-imbalance.js';
export { scanNarrativeMomentum } from './narrative-momentum.js';
export { scanCorrelationBreak } from './correlation-break.js';
export { scanProtocolRevenue } from './protocol-revenue.js';
export { scanFearGreedContrarian } from './fear-greed-contrarian.js';
