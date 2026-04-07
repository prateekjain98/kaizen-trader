"""Strategy registry."""

from src.strategies.momentum import scan_momentum
from src.strategies.mean_reversion import scan_mean_reversion
from src.strategies.listing_pump import on_listing_announcement
from src.strategies.whale_tracker import scan_whale_accumulation
from src.strategies.funding_extreme import scan_funding_extreme
from src.strategies.liquidation_cascade import scan_liquidation_cascade
from src.strategies.orderbook_imbalance import scan_orderbook_imbalance
from src.strategies.narrative_momentum import scan_narrative_momentum
from src.strategies.correlation_break import scan_correlation_break
from src.strategies.protocol_revenue import scan_protocol_revenue
from src.strategies.fear_greed_contrarian import scan_fear_greed_contrarian

__all__ = [
    "scan_momentum", "scan_mean_reversion", "on_listing_announcement",
    "scan_whale_accumulation", "scan_funding_extreme", "scan_liquidation_cascade",
    "scan_orderbook_imbalance", "scan_narrative_momentum", "scan_correlation_break",
    "scan_protocol_revenue", "scan_fear_greed_contrarian",
]
