"""Paper-only order drafting, approval, submission, and monitoring."""

from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.ibkr_client import IbkrClient, IbkrContract


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")


@dataclass(frozen=True)
class PendingOrder:
    token: str
    user_id: str
    account_id: str
    contract: IbkrContract
    side: str
    quantity: int
    limit_price: Decimal
    client_order_id: str
    expires_at: float
    preview: Any

    @property
    def notional(self) -> Decimal:
        return self.limit_price * self.quantity

    def order_payload(self, include_client_order_id: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "acctId": self.account_id,
            "conid": self.contract.conid,
            "orderType": "LMT",
            "price": float(self.limit_price),
            "side": self.side,
            "tif": "DAY",
            "quantity": self.quantity,
            "outsideRTH": False,
        }
        if include_client_order_id:
            payload["cOID"] = self.client_order_id
        return payload


@dataclass(frozen=True)
class PendingBrokerReply:
    token: str
    user_id: str
    order: PendingOrder
    reply_id: str
    messages: tuple[str, ...]


class PaperTradingService:
    def __init__(self, settings: Settings, ibkr_client: IbkrClient):
        self.settings = settings
        self.ibkr_client = ibkr_client
        self._pending_orders: dict[str, PendingOrder] = {}
        self._pending_replies: dict[str, PendingBrokerReply] = {}
        self._daily_notional: dict[str, Decimal] = {}
        self._state_lock = RLock()

    @property
    def has_pending_broker_reply(self) -> bool:
        with self._state_lock:
            return bool(self._pending_replies) or self.ibkr_client.pending_order_reply_id is not None

    def create_order(self, user_id: str, arguments: list[str]) -> str:
        try:
            side, symbol, quantity, limit_price = self._parse_order_arguments(arguments)
            account_id = self._require_paper_trading()
            notional = limit_price * quantity
            self._validate_limits(notional)

            with self._state_lock:
                if any(order.user_id == user_id for order in self._pending_orders.values()):
                    raise RuntimeError("You already have a pending draft. Confirm or cancel it first.")

            contract = self.ibkr_client.resolve_us_stock(symbol)
            self.ibkr_client.market_data_snapshot(contract.conid)
            token = secrets.token_hex(4)
            order = PendingOrder(
                token=token,
                user_id=user_id,
                account_id=account_id,
                contract=contract,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                client_order_id=f"ole-{uuid.uuid4().hex[:24]}",
                expires_at=time.monotonic() + self.settings.ibkr_order_confirmation_ttl_seconds,
                preview={},
            )
            preview = self.ibkr_client.preview_order(
                account_id,
                order.order_payload(include_client_order_id=False),
            )
            error = self._response_error(preview)
            if error:
                raise RuntimeError(f"IBKR What-If rejected the order: {error}")
            order = PendingOrder(**{**order.__dict__, "preview": preview})
            with self._state_lock:
                self._pending_orders[token] = order
            return self._format_draft(order)
        except Exception as exc:
            return f"Order draft failed: {exc}"

    def confirm_order(self, user_id: str, token: str) -> str:
        with self._state_lock:
            order = self._pending_orders.get(token)
            if not order or order.user_id != user_id:
                return "No matching pending order was found."
            if time.monotonic() > order.expires_at:
                self._pending_orders.pop(token, None)
                return "The order confirmation expired. Create a new draft."
            if self.has_pending_broker_reply:
                return "Another IBKR warning is awaiting confirmation. Resolve it first."

        try:
            account_id = self._require_paper_trading()
            if account_id != order.account_id:
                raise RuntimeError("The active paper account changed after the order was previewed.")
            self._validate_limits(order.notional)
            with self._state_lock:
                self._pending_orders.pop(token, None)
                self._reserve_daily_notional(order.notional)
            payload = self.ibkr_client.place_order(
                order.account_id,
                order.order_payload(include_client_order_id=True),
            )
            return self._handle_submission_response(user_id, token, order, payload)
        except Exception as exc:
            return (
                f"Paper order submission failed or has an uncertain result: {exc}\n"
                "Use /orders before creating another order."
            )

    def confirm_broker_reply(self, user_id: str, token: str) -> str:
        with self._state_lock:
            pending = self._pending_replies.get(token)
            if not pending or pending.user_id != user_id:
                return "No matching IBKR warning confirmation was found."

        try:
            payload = self.ibkr_client.confirm_order_reply(pending.reply_id, True)
        except Exception as exc:
            return f"IBKR warning confirmation failed: {exc}"

        with self._state_lock:
            self._pending_replies.pop(token, None)
        return self._handle_submission_response(user_id, token, pending.order, payload)

    def cancel_pending(self, user_id: str, token: str) -> str:
        with self._state_lock:
            order = self._pending_orders.get(token)
            if order and order.user_id == user_id:
                self._pending_orders.pop(token, None)
                return "The order draft was cancelled."
            pending_reply = self._pending_replies.get(token)
            if not pending_reply or pending_reply.user_id != user_id:
                return "No matching pending order or IBKR warning was found."

        try:
            self.ibkr_client.confirm_order_reply(pending_reply.reply_id, False)
        except Exception as exc:
            return f"Could not decline the IBKR warning: {exc}"
        with self._state_lock:
            self._pending_replies.pop(token, None)
        return "The IBKR order warning was declined; the order was not submitted."

    def list_orders(self) -> str:
        try:
            account_id = self._require_paper_trading()
            payload = self.ibkr_client.get_live_orders()
            orders = [
                order
                for order in payload.get("orders", [])
                if str(order.get("acct") or order.get("account") or "").upper() == account_id.upper()
            ]
            if not orders:
                return "IBKR returned no orders for the current paper session."

            lines = [f"Paper orders for {account_id}:"]
            for order in orders[:20]:
                order_id = order.get("orderId") or order.get("order_id") or "unknown"
                symbol = order.get("ticker") or order.get("description1") or "unknown"
                status = order.get("status") or order.get("order_status") or "unknown"
                description = order.get("orderDesc") or order.get("order_description") or ""
                lines.append(f"- {order_id}: {symbol} | {status} | {description}".rstrip(" |"))
            return "\n".join(lines)
        except Exception as exc:
            return f"Order query failed: {exc}"

    def order_status(self, order_id: str) -> str:
        if not order_id.isdigit():
            return "Usage: /order_status <numeric IBKR order id>"
        try:
            account_id = self._require_paper_trading()
            payload = self.ibkr_client.get_order_status(order_id)
            response_account = str(payload.get("account") or payload.get("order_clearing_account") or "")
            if response_account and response_account.upper() != account_id.upper():
                raise RuntimeError("IBKR returned an order from a different account.")
            return "\n".join(
                [
                    f"Paper order {payload.get('order_id', order_id)}:",
                    f"- symbol: {payload.get('symbol') or payload.get('contract_description_1') or 'unknown'}",
                    f"- status: {payload.get('order_status') or 'unknown'}",
                    f"- side: {payload.get('side') or 'unknown'}",
                    f"- total quantity: {payload.get('total_size') or 'unknown'}",
                    f"- filled quantity: {payload.get('cum_fill') or 'unknown'}",
                    f"- average price: {payload.get('average_price') or 'unknown'}",
                    f"- description: {payload.get('order_description') or 'unknown'}",
                ]
            )
        except Exception as exc:
            return f"Order status query failed: {exc}"

    def cancel_live_order(self, order_id: str) -> str:
        if not order_id.isdigit():
            return "Usage: /cancel_order <numeric IBKR order id>"
        try:
            account_id = self._require_paper_trading()
            payload = self.ibkr_client.cancel_order(account_id, order_id)
            error = self._response_error(payload)
            if error:
                raise RuntimeError(error)
            return (
                f"Paper order cancellation requested for {payload.get('order_id', order_id)}.\n"
                "Use /order_status to verify the final status."
            )
        except Exception as exc:
            return f"Order cancellation failed: {exc}"

    def pending_reply_message(self) -> str:
        return (
            "An IBKR order warning is awaiting confirmation. "
            "Other broker requests are blocked; use /confirm_reply <token> or /cancel <token>."
        )

    def _require_paper_trading(self) -> str:
        if self.settings.ibkr_trading_mode != "paper":
            raise RuntimeError("Paper trading is disabled. Set IBKR_TRADING_MODE=paper after the paper account is ready.")
        account_id = self.settings.ibkr_account_id.strip()
        if not account_id:
            raise RuntimeError("IBKR_ACCOUNT_ID must be explicitly set to the DU paper account ID.")
        self.ibkr_client.require_paper_account(account_id)
        return account_id

    def _parse_order_arguments(self, arguments: list[str]) -> tuple[str, str, int, Decimal]:
        if len(arguments) == 5 and arguments[3].upper() == "LMT":
            side_text, symbol_text, quantity_text, _, price_text = arguments
        elif len(arguments) == 4:
            side_text, symbol_text, quantity_text, price_text = arguments
        else:
            raise ValueError("Usage: /order <buy|sell> <symbol> <quantity> [lmt] <limit_price>")

        side = side_text.upper()
        symbol = symbol_text.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("Side must be BUY or SELL.")
        if not SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError("Symbol must be a valid uppercase US stock ticker.")
        try:
            quantity = int(quantity_text)
        except ValueError as exc:
            raise ValueError("Quantity must be a whole number.") from exc
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")
        try:
            limit_price = Decimal(price_text)
        except InvalidOperation as exc:
            raise ValueError("Limit price must be a number.") from exc
        if not limit_price.is_finite() or limit_price <= 0:
            raise ValueError("Limit price must be greater than zero.")
        return side, symbol, quantity, limit_price

    def _validate_limits(self, notional: Decimal) -> None:
        max_order = Decimal(str(self.settings.ibkr_max_order_notional_usd))
        max_daily = Decimal(str(self.settings.ibkr_max_daily_notional_usd))
        if notional > max_order:
            raise RuntimeError(f"Order notional ${notional:,.2f} exceeds the ${max_order:,.2f} per-order limit.")
        used_today = self._daily_notional.get(self._today_key(), Decimal("0"))
        if used_today + notional > max_daily:
            raise RuntimeError(
                f"Order would exceed the ${max_daily:,.2f} daily limit "
                f"(${used_today:,.2f} already reserved today)."
            )

    def _reserve_daily_notional(self, notional: Decimal) -> None:
        today = self._today_key()
        self._daily_notional[today] = self._daily_notional.get(today, Decimal("0")) + notional

    def _today_key(self) -> str:
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()

    def _format_draft(self, order: PendingOrder) -> str:
        lines = [
            "Paper order preview:",
            f"- account: {order.account_id}",
            f"- contract: {order.contract.symbol} — {order.contract.company_name}",
            f"- conid: {order.contract.conid}",
            f"- action: {order.side} {order.quantity} shares",
            f"- type: LMT DAY, regular hours only",
            f"- limit price: ${order.limit_price:,.4f}",
            f"- maximum notional: ${order.notional:,.2f}",
        ]
        preview_lines = self._preview_details(order.preview)
        lines.extend(preview_lines)
        lines.extend(
            [
                "",
                f"Confirm within {self.settings.ibkr_order_confirmation_ttl_seconds} seconds:",
                f"/confirm {order.token}",
                f"Cancel: /cancel {order.token}",
            ]
        )
        return "\n".join(lines)

    def _preview_details(self, payload: Any) -> list[str]:
        item = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else payload
        if not isinstance(item, dict):
            return ["- IBKR What-If: completed"]
        lines: list[str] = []
        for key, label in (
            ("commission", "estimated commission"),
            ("amount", "amount impact"),
            ("equity", "equity impact"),
            ("initial", "initial margin impact"),
            ("maintenance", "maintenance margin impact"),
            ("warn", "warning"),
            ("warning", "warning"),
        ):
            value = item.get(key)
            if value not in (None, "", [], {}):
                lines.append(f"- {label}: {str(value)[:300]}")
        if not lines:
            lines.append("- IBKR What-If: completed")
        return lines

    def _handle_submission_response(
        self,
        user_id: str,
        token: str,
        order: PendingOrder,
        payload: Any,
    ) -> str:
        error = self._response_error(payload)
        if error:
            return f"IBKR rejected the paper order: {error}"

        item = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else payload
        if not isinstance(item, dict):
            return "IBKR returned an unexpected order response. Use /orders to verify status."

        reply_id = item.get("id")
        if reply_id:
            messages = tuple(str(message) for message in item.get("message", []) if message)
            pending = PendingBrokerReply(
                token=token,
                user_id=user_id,
                order=order,
                reply_id=str(reply_id),
                messages=messages,
            )
            with self._state_lock:
                self._pending_replies[token] = pending
            warning_text = "\n".join(f"- {message}" for message in messages) or "- IBKR requires confirmation."
            return (
                "IBKR returned an order warning; the order is not submitted yet:\n"
                f"{warning_text}\n\n"
                "Do not run other broker commands until this is resolved.\n"
                f"Confirm: /confirm_reply {token}\n"
                f"Decline: /cancel {token}"
            )

        order_id = item.get("order_id") or item.get("orderId")
        status = item.get("order_status") or item.get("status") or "unknown"
        if order_id:
            return (
                "Paper order accepted by IBKR.\n"
                f"- order id: {order_id}\n"
                f"- status: {status}\n"
                f"Check: /order_status {order_id}"
            )
        return "IBKR returned an unrecognized order response. Use /orders to verify status."

    def _response_error(self, payload: Any) -> str:
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("error"):
                    return str(item["error"])
        return ""
