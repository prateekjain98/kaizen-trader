"""Binance execution engine — manages orders, positions, and risk.

Fixes applied:
    1. Commission tracking on all paper trades
    2. Trailing stops — stop moves up as price moves in our favor
    3. Server-side stops via Binance OCO orders (live mode)
    4. Proper P&L accounting including fees
    5. Position persistence to JSON (survives restart)
"""

import hmac
import json
import os
import threading
import time
import uuid

import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import env
from src.engine.claude_brain import TradeDecision
from src.engine.log import log


@dataclass
class Position:
    """A live trading position."""
    id: str
    symbol: str
    side: str               # "long" or "short"
    entry_price: float
    size_usd: float
    quantity: float
    stop_pct: float
    target_pct: float
    opened_at: float        # unix ms
    signal_type: str
    reasoning: str
    thesis: str = ""  # human-readable entry thesis
    thesis_conditions: dict = field(default_factory=dict)  # machine-checkable: {"funding_negative": True, "strategy": "funding_squeeze"}
    current_price: float = 0
    high_watermark: float = 0
    low_watermark: float = float("inf")
    entry_commission: float = 0  # fee paid on entry
    trailing_stop_price: float = 0  # moves up with price

    @property
    def stop_price(self) -> float:
        """Current stop — uses trailing stop if it's been moved up.

        Rounded to 10 dp to remove float-precision drift in the multiplication
        (e.g. 100.0 * 1.10 = 110.00000000000001). At real exchange tick sizes
        this is invisible, but it removes a class of off-by-an-epsilon
        comparison bugs in the trigger conditions.
        """
        if self.trailing_stop_price > 0:
            return round(self.trailing_stop_price, 10)
        if self.side == "long":
            return round(self.entry_price * (1 - self.stop_pct), 10)
        return round(self.entry_price * (1 + self.stop_pct), 10)

    @property
    def target_price(self) -> float:
        if self.side == "long":
            return round(self.entry_price * (1 + self.target_pct), 10)
        return round(self.entry_price * (1 - self.target_pct), 10)

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0
        if self.side == "long":
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price

    @property
    def unrealized_pnl_usd(self) -> float:
        """P&L including entry commission (exit commission not yet realized)."""
        return self.unrealized_pnl_pct * self.size_usd - self.entry_commission

    @property
    def hold_hours(self) -> float:
        return (time.time() * 1000 - self.opened_at) / 3_600_000


@dataclass
class ClosedTrade:
    """A completed trade with P&L."""
    position: Position
    exit_price: float
    pnl_pct: float
    pnl_usd: float          # net of ALL commissions
    exit_reason: str
    closed_at: float
    exit_commission: float = 0


_PORTFOLIO_FILE = Path(__file__).parent.parent.parent / "data" / "portfolio.json"


class Executor:
    """Manages trade execution and position lifecycle.

    Features:
        - Commission tracking (0.075% per side with BNB)
        - Trailing stops (move stop up after 1.5x stop distance profit)
        - Server-side OCO orders in live mode
        - Position persistence to JSON
    """

    MAX_POSITIONS = 10
    MAX_POSITION_SIZE = env.max_position_usd
    MAX_DAILY_LOSS = env.max_daily_loss_usd
    COMMISSION_PCT = 0.00075    # Binance with BNB discount
    TRAIL_ACTIVATION = 1.5      # Start trailing after 1.5x stop distance profit

    def __init__(self, paper: bool = True, initial_balance: float = 10_000,
                 trust_initial_balance: bool = False):
        """trust_initial_balance: when True (set by --auto-balance path), the
        balance passed in is fresh from the exchange, so don't let _load_state
        clobber it with a stale value from disk. Open positions still restore.
        Without this, depositing funds during a session has no effect until
        the local state file is manually edited.
        """
        from collections import deque
        from src.risk.protections import ProtectionChain, DEFAULT_PROTECTIONS
        self.paper = paper
        self.balance = initial_balance
        # Layered protections: cooldown after consecutive stops, daily loss
        # cap, max drawdown, rapid-DD halt. Without this only MAX_DAILY_LOSS
        # was enforced (in can_trade) — the engine path bypassed
        # src/risk/portfolio.py entirely. The bot would happily keep entering
        # after 3 consecutive -10% stops with no cooldown.
        self._protections = ProtectionChain.from_config(DEFAULT_PROTECTIONS)
        self._trust_initial_balance = trust_initial_balance
        self.positions: list[Position] = []
        # Bounded so the in-process list cannot grow without limit on a long-running bot.
        # 500 closed trades = ~6+ months at 2-3 trades/day. _save_state still keeps the
        # last 50 on disk; the rest live in Convex.
        self.closed_trades: deque[ClosedTrade] = deque(maxlen=500)
        self.daily_pnl: float = 0
        self.total_commissions: float = 0
        self._daily_reset_ts: float = time.time()
        self._binance = None  # generic exchange provider (binance or okx)
        self._started_at: str = datetime.now(timezone.utc).isoformat()
        self._lock = threading.Lock()
        # Guards against double-close when stop and target both fire in the same
        # update_price tick, or when the price-updater races a brain-driven close.
        self._closing: set[str] = set()

        # Ensure data directory exists
        _PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)

        if not paper:
            from src.config import env
            if env.exchange == "okx":
                from src.execution.providers import OKXProvider
                self._binance = OKXProvider()
                log("info", "Exchange: OKX")
            else:
                from src.execution.providers import BinanceProvider
                self._binance = BinanceProvider()
                log("info", "Exchange: Binance")

        # Load saved state
        self._load_state()

        # Reconcile internal balance with exchange truth at startup. Internal
        # +/- accounting drifts from reality due to fee tier mismatches, fill
        # slippage, and cross-margin model differences. Trust the exchange.
        self._reconcile_balance(reason="startup")

        # Re-emit watchdog stop file from any restored open positions, so a
        # crash/redeploy doesn't leave the watchdog firing at default 15% on
        # positions intended to stop at 5-8%.
        if not self.paper:
            for pos in self.positions:
                self._sync_watchdog_stop(pos)

    # Watchdog stop sync — writes per-symbol stop/target to /tmp/watchdog_stops.json
    # so the watchdog uses the SAME percentages as the bot, not its 15%/40% defaults.
    # Without this, watchdog would only fire 3x past intended stop on -4120-rejected
    # server-side stops (the failure mode currently in production).
    # Shared file readable by the watchdog process. /tmp is isolated per-service
    # (PrivateTmp=true) so a /tmp path would never reach the watchdog.
    _WATCHDOG_STOPS_FILE = str(_PORTFOLIO_FILE.parent / "watchdog_stops.json")

    def _read_watchdog_stops(self) -> dict:
        try:
            with open(self._WATCHDOG_STOPS_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_watchdog_stops_atomic(self, data: dict) -> None:
        tmp = f"{self._WATCHDOG_STOPS_FILE}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._WATCHDOG_STOPS_FILE)
        except Exception as e:
            log("warn", f"Failed to write watchdog stops file: {e}")

    def _sync_watchdog_stop(self, pos: Position) -> None:
        if self.paper:
            return
        data = self._read_watchdog_stops()
        data[pos.symbol] = {"stop": pos.stop_pct, "target": pos.target_pct}
        self._write_watchdog_stops_atomic(data)

    def _clear_watchdog_stop(self, symbol: str) -> None:
        if self.paper:
            return
        data = self._read_watchdog_stops()
        if symbol in data:
            del data[symbol]
            self._write_watchdog_stops_atomic(data)

    def _reconcile_balance(self, reason: str = "periodic") -> None:
        """Overwrite self.balance with the exchange's reported availableBalance.
        Also reconciles position list — any local position that disappeared
        from the exchange (watchdog-closed, manual close, liquidation) is
        force-removed from self.positions so daily_pnl, has_position, and
        capital tracking don't go stale.
        No-op in paper mode or when no provider is configured. Logs drifts > $1
        so silent divergence is visible."""
        if self.paper or self._binance is None:
            return
        try:
            balances = self._binance.get_balances()
        except Exception as e:
            log("warn", f"Balance reconcile fetch failed ({reason}): {e}")
            return
        # Provider returns None on fetch failure (keep stale balance), or {}
        # on no positive balances (genuinely zero — but for a live trading bot
        # that means something's wrong, so still skip the overwrite to avoid
        # stomping a non-zero internal value with a possibly-incomplete API
        # snapshot).
        if balances is None:
            return
        live = balances.get("USDT")
        if live is None or live <= 0:
            return
        with self._lock:
            drift = live - self.balance
            old = self.balance
            self.balance = live
        if abs(drift) >= 1:
            log("info", f"Balance reconciled ({reason}): ${old:.2f} → ${live:.2f} (drift ${drift:+.2f})")
        # Position-state reconcile (Binance only — OKX private WS handles its own).
        if self._binance.name == "binance":
            self._reconcile_positions(reason)

    def _reconcile_positions(self, reason: str) -> None:
        """Drop any local position that no longer exists on the exchange.
        This catches watchdog-closed, manually-closed, or liquidated positions
        that the bot would otherwise still track in self.positions.
        Without this, has_position() blocks re-entry and daily_pnl/protections
        stay blind to the loss."""
        try:
            timestamp = int(time.time() * 1000)
            params = f"timestamp={timestamp}"
            sig = hmac.new(
                env.binance_api_secret.encode(), params.encode(), "sha256"
            ).hexdigest()
            resp = requests.get(
                f"{self._binance.FAPI_BASE}/fapi/v2/positionRisk",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                params={"timestamp": timestamp, "signature": sig},
                timeout=10,
            )
            resp.raise_for_status()
            live_symbols = {
                p["symbol"].replace("USDT", "")
                for p in resp.json() if abs(float(p.get("positionAmt", 0))) > 0
            }
        except Exception as e:
            log("warn", f"Position reconcile fetch failed ({reason}): {e}")
            return

        with self._lock:
            ghosts = [p for p in self.positions if p.symbol not in live_symbols]
            self.positions = [p for p in self.positions if p.symbol in live_symbols]

        for pos in ghosts:
            # We don't know the exit price/PnL — log loudly and clear the
            # watchdog stop entry. Daily PnL stays blind to this trade (better
            # than crediting wrong PnL). Operator should investigate via
            # journal/exchange UI.
            log("warn", f"Position reconcile ({reason}): {pos.symbol} disappeared from exchange "
                f"— removed locally; PnL not credited (check exchange for actual close)")
            self._clear_watchdog_stop(pos.symbol)
        if ghosts:
            self._save_state()

    def _load_state(self):
        """Load portfolio state from JSON if it exists."""
        if _PORTFOLIO_FILE.exists():
            try:
                with open(_PORTFOLIO_FILE) as f:
                    state = json.load(f)
                with self._lock:
                    # Skip restoring balance when caller already fetched it live
                    # from the exchange (--auto-balance) — disk state would be stale.
                    if not self._trust_initial_balance:
                        self.balance = state.get("balance", self.balance)
                    self.total_commissions = state.get("total_commissions", 0)
                    # Restore positions
                    for p in state.get("positions", []):
                        self.positions.append(Position(
                            id=p.get("id", str(uuid.uuid4())),
                            symbol=p["symbol"], side=p["side"],
                            entry_price=p["entry_price"], size_usd=p["size_usd"],
                            quantity=p.get("quantity", p["size_usd"] / p["entry_price"]),
                            stop_pct=p["stop_pct"], target_pct=p.get("target_pct", 0.15),
                            opened_at=p["opened_at"], signal_type=p.get("signal_type", ""),
                            reasoning=p.get("reasoning", ""),
                            thesis=p.get("thesis", ""),
                            thesis_conditions=p.get("thesis_conditions", {}),
                            current_price=p.get("current_price", p["entry_price"]),
                            high_watermark=p.get("high_watermark", p["entry_price"]),
                            entry_commission=p.get("entry_commission", 0),
                            trailing_stop_price=p.get("trailing_stop_price", 0),
                        ))
                    log("info", f"Loaded portfolio: ${self.balance:,.2f}, {len(self.positions)} positions")
            except Exception as e:
                log("warn", f"Failed to load portfolio state: {e}")

    def _save_state(self):
        """Persist portfolio to JSON."""
        with self._lock:
            state = {
                "balance": self.balance,
                "total_commissions": self.total_commissions,
                "positions": [
                    {
                        "id": p.id, "symbol": p.symbol, "side": p.side,
                        "entry_price": p.entry_price, "size_usd": p.size_usd,
                        "quantity": p.quantity, "stop_pct": p.stop_pct,
                        "target_pct": p.target_pct, "opened_at": p.opened_at,
                        "signal_type": p.signal_type, "reasoning": p.reasoning,
                        "thesis": p.thesis, "thesis_conditions": p.thesis_conditions,
                        "current_price": p.current_price,
                        "high_watermark": p.high_watermark,
                        "entry_commission": p.entry_commission,
                        "trailing_stop_price": p.trailing_stop_price,
                        "stop_price": p.stop_price,
                        "target_price": p.target_price,
                    }
                    for p in self.positions
                ],
                "closed_trades": [
                    {
                        "symbol": t.position.symbol, "side": t.position.side,
                        "entry": t.position.entry_price, "exit": t.exit_price,
                        "pnl_pct": t.pnl_pct, "pnl_usd": t.pnl_usd,
                        "reason": t.exit_reason, "closed_at": t.closed_at,
                        "commissions": t.position.entry_commission + t.exit_commission,
                    }
                    for t in list(self.closed_trades)[-50:]  # deque needs list() before slicing
                ],
                "total_pnl": sum(t.pnl_usd for t in self.closed_trades),
                "total_commissions": self.total_commissions,
                "started_at": self._started_at,
            }
        with open(_PORTFOLIO_FILE, "w") as f:
            json.dump(state, f, indent=2)

    def _reset_daily(self):
        now = time.time()
        if now - self._daily_reset_ts > 86400:
            self.daily_pnl = 0
            self._daily_reset_ts = now

    def can_trade(self) -> bool:
        self._reset_daily()
        if len(self.positions) >= self.MAX_POSITIONS:
            return False
        if self.daily_pnl <= -self.MAX_DAILY_LOSS:
            return False
        if self.balance < 5:  # Allow trading on small accounts
            return False
        # Run the full protection chain (cooldown after stops, drawdown halt,
        # rapid-DD halt). Logs the rule that blocked entry so behavior is
        # auditable.
        from src.risk.protections import ProtectionContext
        ctx = ProtectionContext(
            realized_pnl_today=self.daily_pnl,
            open_position_count=len(self.positions),
            timestamp_ms=time.time() * 1000,
        )
        verdict = self._protections.check(ctx)
        if not verdict.allowed:
            log("info", f"Trade blocked by protection: {verdict.rule_name} — {verdict.reason}")
            return False
        return True

    def has_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def open_position(self, decision: TradeDecision) -> Optional[Position]:
        """Open a new position with commission tracking."""
        # Reconcile before sizing so we never undersize/oversize against a
        # stale internal balance.
        self._reconcile_balance(reason="pre-open")
        with self._lock:
            if not self.can_trade():
                return None
            if self.has_position(decision.symbol):
                return None

            size = min(decision.size_usd, self.MAX_POSITION_SIZE, self.balance * 0.4)
            if size < 10:
                return None

            entry_commission = size * self.COMMISSION_PCT
            if size + entry_commission > self.balance:
                return None

            entry_price = decision.entry_price
            if entry_price <= 0:
                return None

            quantity = size / entry_price

        # Execute on exchange (live mode) — outside lock to avoid blocking
        if not self.paper and self._binance:
            try:
                side_str = "BUY" if decision.side == "long" else "SELL"
                # OKX supports attached server-side OCO (SL+TP) on the entry order
                # itself, atomic with the fill. Binance requires a separate call
                # after the entry — handled below via _place_server_side_stops.
                attach_sl = attach_tp = None
                if self._binance.name == "okx":
                    if decision.side == "long":
                        attach_sl = entry_price * (1 - decision.stop_pct)
                        attach_tp = entry_price * (1 + decision.target_pct)
                    else:
                        attach_sl = entry_price * (1 + decision.stop_pct)
                        attach_tp = entry_price * (1 - decision.target_pct)
                    trade = self._binance._place_order(
                        decision.symbol, decision.signal_id, side_str.lower(),
                        quantity, entry_price,
                        attach_sl_px=attach_sl, attach_tp_px=attach_tp,
                    )
                else:
                    trade = self._binance._place_order(
                        decision.symbol, decision.signal_id, side_str, quantity, entry_price
                    )
                if trade.status == "failed":
                    log("warn", f"{self._binance.name} order failed: {decision.symbol}: {trade.error}")
                    return None
                entry_price = trade.price if trade.price > 0 else entry_price
                quantity = trade.quantity if trade.quantity > 0 else quantity
            except Exception as e:
                log("error", f"Execution error: {decision.symbol}: {e}")
                return None

            # Stop placement is best-effort — if it raises, the position is
            # ALREADY filled on the exchange; we MUST still record it locally
            # (otherwise it becomes a ghost position with no bot tracking and
            # no watchdog stop entry). The watchdog file written below is the
            # safety net when -4120 rejects server-side stops.
            if self._binance.name != "okx":
                try:
                    self._place_server_side_stops(decision.symbol, decision.side,
                                                   quantity, entry_price, decision.stop_pct, decision.target_pct)
                except Exception as e:
                    log("error", f"Server-side stops failed for {decision.symbol}: {e} "
                        f"— position will be tracked locally; watchdog handles risk")

        with self._lock:
            self.balance -= (size + entry_commission)
            self.total_commissions += entry_commission

            pos = Position(
                id=str(uuid.uuid4()), symbol=decision.symbol, side=decision.side,
                entry_price=entry_price, size_usd=size, quantity=quantity,
                stop_pct=decision.stop_pct, target_pct=decision.target_pct,
                opened_at=time.time() * 1000,
                signal_type=decision.reasoning[:50], reasoning=decision.reasoning,
                thesis=decision.reasoning,
                thesis_conditions=getattr(decision, 'thesis_conditions', {}),
                current_price=entry_price, high_watermark=entry_price,
                entry_commission=entry_commission,
            )

            self.positions.append(pos)

        self._save_state()
        # Sync per-symbol stop/target into watchdog file so the watchdog uses
        # the SAME thresholds as the bot (not its 15%/40% defaults).
        self._sync_watchdog_stop(pos)
        log("trade", f"OPEN {pos.side.upper()} {pos.symbol} ${size:.0f} @ ${entry_price:.4f} "
            f"stop=${pos.stop_price:.4f} target=${pos.target_price:.4f} fee=${entry_commission:.3f}")
        return pos

    def _place_server_side_stops(self, symbol: str, side: str, quantity: float,
                                   entry: float, stop_pct: float, target_pct: float):
        """Place stop loss and take profit orders on Binance server.
        These execute even if our process crashes.
        OKX uses a different OCO mechanism — skipped for now."""
        if not self._binance or self.paper:
            return

        # Only Binance supports this code path; OKX uses a different OCO mechanism
        if not hasattr(self._binance, '_get_binance_symbol'):
            log("warn", f"Server-side stops not implemented for {self._binance.name} — skipping for {symbol}")
            return

        import requests

        binance_symbol = self._binance._get_binance_symbol(symbol)
        close_side = "SELL" if side == "long" else "BUY"
        stop_price = entry * (1 - stop_pct) if side == "long" else entry * (1 + stop_pct)
        tp_price = entry * (1 + target_pct) if side == "long" else entry * (1 - target_pct)

        timestamp = int(time.time() * 1000)

        # Stop loss order. Binance India accounts now reject closePosition=true with
        # STOP_MARKET on /fapi/v1/order with -4120; use explicit quantity + reduceOnly
        # which is the supported pattern across Binance Futures regions.
        try:
            sl_params = (
                f"symbol={binance_symbol}&side={close_side}&type=STOP_MARKET"
                f"&stopPrice={stop_price:.8f}&quantity={quantity}&reduceOnly=true"
                f"&workingType=MARK_PRICE&timeInForce=GTC"
                f"&timestamp={timestamp}"
            )
            signature = hmac.new(
                env.binance_api_secret.encode(), sl_params.encode(), "sha256"
            ).hexdigest()
            r = requests.post(
                f"{self._binance.FAPI_BASE}/fapi/v1/order",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                data=f"{sl_params}&signature={signature}",
                timeout=10,
            )
            # Binance returns 4xx for rejected orders without raising — must check status.
            # Previous code logged success unconditionally, which masked silent failures.
            if r.status_code == 200:
                log("info", f"Server-side stop placed: {symbol} @ ${stop_price:.4f} orderId={r.json().get('orderId')}")
            else:
                log("error", f"Server-side stop REJECTED for {symbol}: {r.status_code} {r.text[:300]}")
        except Exception as e:
            log("error", f"Server-side stop EXCEPTION for {symbol}: {e}")

        # Take profit order — re-capture timestamp. The original timestamp can
        # easily fall outside Binance's recvWindow (5s default) if the SL POST
        # took >5s, causing a silent -1021 rejection where the TP never lands.
        try:
            tp_timestamp = int(time.time() * 1000)
            tp_params = (
                f"symbol={binance_symbol}&side={close_side}&type=TAKE_PROFIT_MARKET"
                f"&stopPrice={tp_price:.8f}&quantity={quantity}&reduceOnly=true"
                f"&workingType=MARK_PRICE&timeInForce=GTC"
                f"&timestamp={tp_timestamp}"
            )
            signature = hmac.new(
                env.binance_api_secret.encode(), tp_params.encode(), "sha256"
            ).hexdigest()
            r = requests.post(
                f"{self._binance.FAPI_BASE}/fapi/v1/order",
                headers={"X-MBX-APIKEY": env.binance_api_key},
                data=f"{tp_params}&signature={signature}",
                timeout=10,
            )
            if r.status_code == 200:
                log("info", f"Server-side TP placed: {symbol} @ ${tp_price:.4f} orderId={r.json().get('orderId')}")
            else:
                log("error", f"Server-side TP REJECTED for {symbol}: {r.status_code} {r.text[:300]}")
        except Exception as e:
            log("error", f"Server-side TP EXCEPTION for {symbol}: {e}")

    def update_price(self, symbol: str, price: float):
        """Update price, check stops/targets, and manage trailing stops.

        Note: chop exits are handled by the brain (RuleBrain._check_chop_exits
        or ClaudeBrain tick) to avoid double-closes.
        """
        with self._lock:
            positions_snapshot = list(self.positions)

        for pos in positions_snapshot:
            if pos.symbol != symbol:
                continue
            pos.current_price = price
            pos.high_watermark = max(pos.high_watermark, price)
            pos.low_watermark = min(pos.low_watermark, price)

            # --- Trailing stop logic ---
            # Activate trailing after price moves 1.5x the stop distance in our favor
            trail_activation_dist = pos.stop_pct * self.TRAIL_ACTIVATION
            if pos.side == "long":
                profit_pct = (price - pos.entry_price) / pos.entry_price
                if profit_pct > trail_activation_dist:
                    # Trail stop at entry + (profit - stop_pct)
                    new_trail = price * (1 - pos.stop_pct)
                    if new_trail > pos.trailing_stop_price:
                        pos.trailing_stop_price = new_trail
            else:
                profit_pct = (pos.entry_price - price) / pos.entry_price
                if profit_pct > trail_activation_dist:
                    new_trail = price * (1 + pos.stop_pct)
                    if pos.trailing_stop_price == 0 or new_trail < pos.trailing_stop_price:
                        pos.trailing_stop_price = new_trail

            # Check stop loss (uses trailing if active)
            if pos.side == "long" and price <= pos.stop_price:
                self._close_position(pos, price, "stop")
            elif pos.side == "short" and price >= pos.stop_price:
                self._close_position(pos, price, "stop")

            # Check take profit
            elif pos.side == "long" and price >= pos.target_price:
                self._close_position(pos, price, "target")
            elif pos.side == "short" and price <= pos.target_price:
                self._close_position(pos, price, "target")

            # Max hold time (48h)
            elif pos.hold_hours > 48:
                self._close_position(pos, price, "timeout")

    def _close_position(self, pos: Position, exit_price: float, reason: str):
        """Close position with full commission accounting.

        Race-safe: a position can only be closed once. Concurrent calls (e.g.
        stop and target firing in the same tick, or brain + price-updater
        racing) are deduped via the _closing guard set.

        Money-safe: if the live exchange close fails, we do NOT credit P&L or
        remove the position locally — the position is still live on the
        exchange and the watchdog will retry. Marking it closed locally would
        orphan the live position with no tracking.
        """
        # Atomic claim: only one caller proceeds past this point per pos.id.
        with self._lock:
            if pos.id in self._closing:
                return
            if not any(p.id == pos.id for p in self.positions):
                return  # already closed
            self._closing.add(pos.id)

        try:
            exit_commission = pos.size_usd * self.COMMISSION_PCT

            if pos.side == "long":
                pnl_pct = (exit_price - pos.entry_price) / pos.entry_price
            else:
                pnl_pct = (pos.entry_price - exit_price) / pos.entry_price

            gross_pnl = pos.size_usd * pnl_pct
            pnl_usd = gross_pnl - pos.entry_commission - exit_commission

            # Execute close on exchange (live mode). On failure: ABORT — do not
            # update local state, do not credit P&L. The position is still live
            # on the exchange; the watchdog will retry the close.
            if not self.paper and self._binance:
                try:
                    close_side = "SELL" if pos.side == "long" else "BUY"
                    trade = self._binance._place_order(
                        pos.symbol, pos.id, close_side, pos.quantity, exit_price,
                        reduce_only=True,
                    )
                    if trade.status != "filled":
                        log("error", f"Close FAILED for {pos.symbol}: {trade.error or 'unknown'} "
                            f"— position remains open, watchdog will retry")
                        return  # Do NOT update local state
                except Exception as e:
                    log("error", f"Close execution error: {pos.symbol}: {e} — position remains open")
                    return

            with self._lock:
                self.total_commissions += exit_commission
                self.balance += pos.size_usd + pnl_usd
                self.daily_pnl += pnl_usd

                closed = ClosedTrade(
                    position=pos, exit_price=exit_price,
                    pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                    exit_reason=reason, closed_at=time.time() * 1000,
                    exit_commission=exit_commission,
                )
                self.closed_trades.append(closed)
                self.positions = [p for p in self.positions if p.id != pos.id]

            self._save_state()
        finally:
            with self._lock:
                self._closing.discard(pos.id)

        # Reconcile after settle so the next sizing call uses the real
        # post-close exchange balance, not our rough internal math.
        self._reconcile_balance(reason="post-close")
        # Drop watchdog stop entry so it doesn't fire on a re-opened symbol
        # using the previous trade's parameters.
        self._clear_watchdog_stop(pos.symbol)

        # Feed the protection chain so StoplossGuard / Cooldown / Drawdown
        # rules update their state. Without this they never fire on the
        # engine path.
        try:
            pos.exit_reason = reason  # ensure StoplossGuard sees the reason
            self._protections.notify_close(pos, pnl_usd)
        except Exception as e:
            log("warn", f"Protection chain notify_close failed: {e}")

        trail_info = f" (trailed from ${pos.entry_price*(1-pos.stop_pct):.4f} to ${pos.trailing_stop_price:.4f})" if pos.trailing_stop_price > 0 else ""
        log("trade", f"CLOSE {pos.symbol} {reason} ${pnl_usd:+.2f} ({pnl_pct*100:+.2f}%) "
            f"held {pos.hold_hours:.1f}h fees=${pos.entry_commission + exit_commission:.3f}{trail_info} "
            f"| Bal: ${self.balance:,.2f}")

    def get_stats(self) -> dict:
        total_trades = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t.pnl_usd > 0)
        total_pnl = sum(t.pnl_usd for t in self.closed_trades)
        unrealized = sum(p.unrealized_pnl_usd for p in self.positions)

        return {
            "balance": self.balance,
            "open_positions": len(self.positions),
            "total_trades": total_trades,
            "wins": wins,
            "win_rate": wins / total_trades * 100 if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "unrealized_pnl": unrealized,
            "daily_pnl": self.daily_pnl,
            "total_commissions": self.total_commissions,
        }
