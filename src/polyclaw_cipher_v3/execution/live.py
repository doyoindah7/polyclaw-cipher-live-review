"""Live executor - wraps py-clob-client-v2 for real Polymarket trading.

Implements BaseExecutor interface. Uses:
- signature_type=3 (POLY_1271 / EIP-7702)
- Deposit wallet as funder
- L2 API credentials for auth

Env vars required:
  POLYGON_RPC_URL
  PRIVATE_KEY
  POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE
  LIVE_FUNDER (deposit wallet address)
"""
from __future__ import annotations

import asyncio, json, logging, os, time, uuid
from typing import Any, Optional

from ..core.types import Position, Signal, Trade, Side

logger = logging.getLogger(__name__)


class LiveExecutor:
    """Live Polymarket executor via CLOB V2 API."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self._client = None
        self._initialized = False

        # Required env vars
        self._private_key = os.environ.get("PRIVATE_KEY", "")
        self._funder = os.environ.get("LIVE_FUNDER", os.environ.get("BOT_ADDRESS", ""))
        self._l2_key = os.environ.get("POLYMARKET_API_KEY", "")
        self._l2_secret = os.environ.get("POLYMARKET_API_SECRET", "")
        self._l2_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "")

        # Fee rate in bps (0.25% taker = 25 bps)
        self._fee_rate_bps = self.config.get("fee_rate_bps", 25)

        # Execution tracking
        self._pending_orders: dict[str, dict[str, Any]] = {}
        self._order_latency: list[float] = []
        self._pending_close_tokens: set[str] = set()  # v3.6.1: prevent retry on live SELL

        if not all([self._private_key, self._l2_key]):
            logger.warning(
                "LiveExecutor: missing PRIVATE_KEY or POLYMARKET_API_KEY - "
                "live trading disabled. Falling back to paper."
            )
            self.enabled = False
        else:
            self.enabled = True

    def _ensure_client(self):
        """Lazy-init CLOB client."""
        if self._initialized and self._client is not None:
            return self._client

        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self._l2_key,
                api_secret=self._l2_secret,
                api_passphrase=self._l2_passphrase,
            )

            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._private_key,
                chain_id=137,
                creds=creds,
                signature_type=3,  # POLY_1271
                funder=self._funder,
            )

            self._initialized = True
            logger.info(
                "LiveExecutor: CLOB V2 client initialized "
                "(sig_type=3, funder=%s...%s)",
                self._funder[:10], self._funder[-6:],
            )
            return self._client

        except Exception as e:
            logger.error("LiveExecutor: failed to init CLOB client: %s", e)
            raise

    # ---- Public API ----

    async def execute_entry(
        self, signal: Signal, market_question: str, bankroll: float, **kwargs
    ) -> Position | None:
        """Place live BUY order. Returns Position if filled, None if not.

        SAFETY: Caller must check wallet.available_cash BEFORE calling this.
        This method does a final sanity check on order size.
        """
        if not self.enabled:
            logger.warning("LiveExecutor: disabled, skipping entry")
            return None

        client = self._ensure_client()
        token_id = signal.token_id or signal.legs[0].token_id if signal.legs else ""
        if not token_id:
            logger.error("LiveExecutor: no token_id in signal")
            return None

        # SAFETY: Reject if order size exceeds bankroll (last-resort guard)
        order_usd = signal.suggested_size_usd
        if order_usd > bankroll:
            logger.error(
                "LIVE ENTRY REJECTED: order $%.2f > bankroll $%.2f - DANGER GUARD",
                order_usd, bankroll,
            )
            return None

        # Entry is ALWAYS BUY - either buy YES token or buy NO token
        # The token_id identifies which outcome. SELL = exit, not entry.
        side_str = "BUY"
        price = signal.suggested_price
        size = self._usd_to_shares(order_usd, price)

        if size <= 0:
            logger.error("LIVE ENTRY REJECTED: size=0 (price=%.3f, usd=%.2f)", price, order_usd)
            return None

        # CLOB V2 minimum order size = 5 shares
        MIN_ORDER_SIZE = 5
        if size < MIN_ORDER_SIZE:
            logger.warning(
                "LIVE ENTRY REJECTED: size %.2f < minimum %d shares (price=%.3f, usd=%.2f). "
                "Need at least $%.2f for this price.",
                size, MIN_ORDER_SIZE, price, order_usd, MIN_ORDER_SIZE * price,
            )
            return None

        logger.info(
            "LIVE ENTRY: %s %s %.0f @ %.3f ($%.2f) | %s",
            side_str, token_id[:12], size, price,
            order_usd, market_question[:50],
        )

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            side_const = BUY  # Always BUY for entry - token_id determines YES/NO outcome

            # Get tick size + neg_risk
            try:
                tick_size_str = str(client.get_tick_size(token_id))
            except Exception:
                tick_size_str = "0.01"

            try:
                neg_risk = client.get_neg_risk(token_id)
            except Exception:
                neg_risk = False

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_const,
            )
            options = CreateOrderOptions(tick_size=tick_size_str, neg_risk=neg_risk)

            t0 = time.time()
            signed = client.create_order(order_args, options)
            t_sign = (time.time() - t0) * 1000

            t1 = time.time()
            result = client.post_order(signed, OrderType.GTC)
            t_post = (time.time() - t1) * 1000

            self._order_latency.append((time.time() - t0) * 1000)

            status = result.get("status", "?")
            order_id = result.get("orderID", "")
            tx_hashes = result.get("transactionsHashes", [])

            logger.info(
                "LIVE ORDER: %s | status=%s | sign=%.0fms post=%.0fms | id=%s",
                order_id[:16] if order_id else "no-id",
                status, t_sign, t_post, tx_hashes,
            )

            if status == "matched":
                # Order filled - position is real
                self._pending_orders[order_id] = {
                    "token_id": token_id,
                    "side": side_str,
                    "price": price,
                    "size": size,
                    "market_question": market_question,
                    "signal_id": signal.id,
                    "strategy": signal.strategy_name,
                    "created_at": time.time(),
                    "status": status,
                }
            elif status == "live":
                # Order placed but NOT filled (limit order on book)
                # v3.5.17: DON'T cancel - let GTC order sit on book to fill
                # Reconcile will track if it fills via CLOB balance change
                logger.info(
                    "LIVE ORDER on book (status=live) %s | %d shares @ $%.4f = $%.2f | waiting for match...",
                    order_id[:12] if order_id else "?", int(shares), price, size_usd,
                )
                return None  # Don't debit cash until filled
            else:
                logger.warning("LIVE ORDER unknown status: %s - treating as failed", status)
                return None

            # Build Position ONLY for matched orders
            pos_id = f"live-{uuid.uuid4().hex[:8]}"
            return Position(
                id=pos_id,
                market_condition_id=signal.market_condition_id,
                market_question=market_question,
                side=signal.side,
                token_id=token_id,
                entry_price=price,
                shares=size,
                invested=signal.suggested_size_usd,
                strategy=signal.strategy_name,
                opened_at=time.time(),
            )

        except Exception as e:
            logger.error("LIVE ENTRY FAILED: %s", e)
            return None

    async def close_position(
        self, pos: Position, exit_price: float, reason: str, **kwargs
    ) -> Trade:
        """Close position at given price (TP/SL/max hold)."""
        if not self.enabled:
            return self._fake_trade(pos, exit_price, reason)

        # CLOB V2 minimum order size = 5 shares
        if pos.shares < 5:
            logger.warning(
                "LIVE CLOSE SKIP: position %s has %.2f shares < minimum 5 - "
                "cannot sell on CLOB, will resolve at market close",
                pos.id[:8], pos.shares,
            )
            return None  # Can't close, position too small for CLOB

        client = self._ensure_client()
        token_id = pos.token_id
        exit_side = "SELL" if pos.side == Side.YES else "BUY"

        # v3.6.1: Skip if already has a live SELL on book (prevent allowance retry loop)
        if token_id in self._pending_close_tokens:
            logger.debug("LIVE CLOSE SKIP: already live on book for %s", token_id[:12])
            return None

        logger.info(
            "LIVE CLOSE: %s %s %.0f @ %.3f | %s",
            exit_side, token_id[:12], pos.shares, exit_price, reason,
        )

        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY, SELL

            side_const = SELL if pos.side == Side.YES else BUY

            tick_size_str = "0.01"
            neg_risk = False
            try:
                tick_size_str = str(client.get_tick_size(token_id))
                neg_risk = client.get_neg_risk(token_id)
            except Exception:
                pass

            order_args = OrderArgs(
                token_id=token_id,
                price=exit_price,
                size=pos.shares,
                side=side_const,
            )
            options = CreateOrderOptions(tick_size=tick_size_str, neg_risk=neg_risk)

            t0 = time.time()
            signed = client.create_order(order_args, options)
            t_sign = (time.time() - t0) * 1000

            t1 = time.time()
            result = client.post_order(signed, OrderType.GTC)
            t_post = (time.time() - t1) * 1000

            self._order_latency.append((time.time() - t0) * 1000)

            status = result.get("status", "?")
            making_amount = float(result.get("makingAmount", 0) or 0)

            if status not in ("matched", "live"):
                logger.error("LIVE CLOSE unknown status: %s - treating as failed", status)
                return None

            # For matched orders, use makingAmount. For live (unfilled), don't close/cancel.
            # v3.5.17: Leave GTC sell on book - it will fill eventually
            if status == "live":
                self._pending_close_tokens.add(token_id)  # v3.6.1: prevent retry
                logger.info("LIVE CLOSE order on book (status=live) - waiting for fill...")
                return None  # Position stays open, order on book, bot will retry/reconcile

            # Matched - clear pending
            self._pending_close_tokens.discard(token_id)
            pnl_dollar = making_amount - pos.invested
            pnl_pct = (pnl_dollar / pos.invested * 100) if pos.invested > 0 else 0

            return Trade(
                id=f"close-{uuid.uuid4().hex[:8]}",
                market_condition_id=pos.market_condition_id,
                market_question=pos.market_question,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                shares=pos.shares,
                invested=pos.invested,
                pnl_dollar=round(pnl_dollar, 4),
                pnl_percent=round(pnl_pct, 2),
                opened_at=pos.opened_at,
                closed_at=time.time(),
                strategy=pos.strategy,
                reason=reason,
            )

        except Exception as e:
            self._pending_close_tokens.add(token_id)  # v3.6.1: prevent retry loop
            logger.error("LIVE CLOSE FAILED: %s (exit_price=%.3f) - position stays open", e, exit_price)
            return None  # Don't fake-close; position remains open for retry

    async def resolve_position(
        self, pos: Position, winning_side: str
    ) -> Trade:
        """Resolve position at market close (win/lose payout)."""
        # For resolved markets, the exit price is 1.0 if won, 0.0 if lost
        won = (pos.side == Side.YES and winning_side == "YES") or \
              (pos.side == Side.NO and winning_side == "NO")
        exit_price = 1.0 if won else 0.0
        return await self.close_position(pos, exit_price, f"resolved:{winning_side}")

    # ---- Helpers ----

    def _usd_to_shares(self, usd_amount: float, price: float) -> float:
        """Convert USD amount to share count (including fee buffer)."""
        if price <= 0 or price >= 1:
            return 0
        fee = self._fee_rate_bps / 10000  # bps → decimal
        return usd_amount / (price * (1 + fee))

    def _fake_trade(self, pos: Position, exit_price: float, reason: str) -> Trade:
        """Fallback trade when live executor disabled."""
        pnl = (exit_price - pos.entry_price) * pos.shares
        return Trade(
            id=f"fake-{uuid.uuid4().hex[:8]}",
            market_condition_id=pos.market_condition_id,
            market_question=pos.market_question,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            invested=pos.invested,
            pnl_dollar=round(pnl, 4),
            pnl_percent=round(pnl / pos.invested * 100, 2) if pos.invested > 0 else 0,
            opened_at=pos.opened_at,
            closed_at=time.time(),
            strategy=pos.strategy,
            reason=f"{reason} [fallback]",
        )

    async def get_clob_balance(self) -> float:
        """Query live CLOB balance (in USD)."""
        if not self.enabled:
            return 0.0
        try:
            client = self._ensure_client()
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            bal = await asyncio.to_thread(
                client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            return int(bal.get("balance", "0")) / 1_000_000 if isinstance(bal, dict) else 0
        except Exception as e:
            logger.error("LiveExecutor: get_clob_balance failed: %s", e)
            return 0.0

    def avg_latency_ms(self) -> float:
        """Average order latency in ms."""
        if not self._order_latency:
            return 0
        return sum(self._order_latency) / len(self._order_latency)
