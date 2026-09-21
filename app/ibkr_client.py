"""Interactive Brokers Client Portal Gateway client."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
import time
from typing import Any

import requests
import urllib3


class IbkrApiError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int):
        super().__init__(f"IBKR {method} {path} failed with HTTP {status_code}.")
        self.status_code = status_code


class IbkrPendingReplyError(RuntimeError):
    pass


@dataclass(frozen=True)
class IbkrAccount:
    account_id: str
    display_name: str
    currency: str


@dataclass(frozen=True)
class IbkrContract:
    conid: int
    symbol: str
    company_name: str
    currency: str
    listing_exchange: str


@dataclass(frozen=True)
class IbkrQuote:
    conid: int
    last: str | None
    bid: str | None
    ask: str | None
    bid_size: str | None
    ask_size: str | None
    last_size: str | None
    data_mode: str
    updated_at: str | None


@dataclass(frozen=True)
class IbkrHistoricalBar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None


class IbkrClient:
    def __init__(self, base_url: str, verify_ssl: bool = False, account_id: str = ""):
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.account_id = account_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Host": "api.ibkr.com",
                "User-Agent": "ole-trading-assistant/1.0",
                "Accept": "*/*",
                "Connection": "keep-alive",
            }
        )
        self._portfolio_accounts_primed = False
        self._request_lock = RLock()
        self._pending_order_reply_id: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    def auth_status(self) -> dict[str, Any]:
        return self._request("POST", "/iserver/auth/status")

    def validate_sso(self) -> dict[str, Any]:
        return self._request("GET", "/sso/validate")

    def tickle(self) -> dict[str, Any]:
        return self._request("POST", "/tickle")

    @property
    def pending_order_reply_id(self) -> str | None:
        with self._request_lock:
            return self._pending_order_reply_id

    def get_brokerage_accounts(self) -> dict[str, Any]:
        return self._request("GET", "/iserver/accounts")

    def require_paper_account(self, account_id: str) -> dict[str, Any]:
        if not account_id.upper().startswith("DU"):
            raise RuntimeError("Trading is locked to an IBKR paper account whose ID starts with DU.")

        payload = self.get_brokerage_accounts()
        account_ids = {str(value).upper() for value in payload.get("accounts", [])}
        if account_id.upper() not in account_ids:
            raise RuntimeError("The configured paper account is not available in the current Gateway session.")

        account_properties = payload.get("acctProps", {}).get(account_id, {})
        is_paper = payload.get("isPaper") is True or account_properties.get("isPaper") is True
        if not is_paper:
            raise RuntimeError("IBKR did not confirm that the active brokerage session is a paper session.")
        return payload

    def resolve_us_stock(self, symbol: str) -> IbkrContract:
        search_results = self._request(
            "GET",
            "/iserver/secdef/search",
            params={"symbol": symbol, "name": "false"},
        )
        candidates = [
            item
            for item in search_results
            if str(item.get("symbol", "")).upper() == symbol.upper()
            and str(item.get("secType", "STK")).upper() == "STK"
            and item.get("restricted") is not True
        ]

        contracts: list[IbkrContract] = []
        for candidate in candidates:
            conid = int(candidate.get("conid") or 0)
            if not conid:
                continue
            details_payload = self._request(
                "GET",
                "/trsrv/secdef",
                params={"conids": str(conid)},
            )
            details_items = details_payload.get("secdef", []) if isinstance(details_payload, dict) else []
            details = details_items[0] if details_items else {}
            if (
                str(details.get("currency", "")).upper() != "USD"
                or details.get("isUS") is not True
            ):
                continue
            contracts.append(
                IbkrContract(
                    conid=conid,
                    symbol=symbol.upper(),
                    company_name=str(
                        details.get("name")
                        or candidate.get("companyName")
                        or candidate.get("companyHeader")
                        or symbol.upper()
                    ).strip(),
                    currency="USD",
                    listing_exchange=str(
                        details.get("listingExchange")
                        or candidate.get("description")
                        or "SMART"
                    ),
                )
            )

        unique_contracts = {contract.conid: contract for contract in contracts}
        if not unique_contracts:
            raise RuntimeError(f"No unrestricted USD stock contract was found for {symbol.upper()}.")
        if len(unique_contracts) > 1:
            matches = ", ".join(
                f"{contract.company_name} ({contract.listing_exchange}, conid={contract.conid})"
                for contract in list(unique_contracts.values())[:3]
            )
            raise RuntimeError(f"The symbol is ambiguous. Matching contracts: {matches}")
        return next(iter(unique_contracts.values()))

    def market_data_snapshot(self, conid: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/iserver/marketdata/snapshot",
            params={"conids": str(conid), "fields": "31,84,85,86,88,6509,7059"},
        )
        return payload if isinstance(payload, list) else []

    def get_quote(self, conid: int) -> IbkrQuote:
        snapshots = self.market_data_snapshot(conid)
        snapshot = self._snapshot_for_conid(snapshots, conid)
        if not self._has_quote_values(snapshot):
            time.sleep(0.5)
            payload = self._request(
                "GET",
                "/iserver/marketdata/snapshot",
                params={"conids": str(conid)},
            )
            snapshots = payload if isinstance(payload, list) else []
            snapshot = self._snapshot_for_conid(snapshots, conid)

        return IbkrQuote(
            conid=conid,
            last=self._optional_string(snapshot.get("31")),
            bid=self._optional_string(snapshot.get("84")),
            ask=self._optional_string(snapshot.get("86")),
            bid_size=self._optional_string(snapshot.get("88")),
            ask_size=self._optional_string(snapshot.get("85")),
            last_size=self._optional_string(snapshot.get("7059")),
            data_mode=self._market_data_mode(snapshot.get("6509")),
            updated_at=self._format_market_timestamp(snapshot.get("_updated")),
        )

    def get_historical_bars(
        self,
        conid: int,
        *,
        period: str,
        bar: str,
        outside_rth: bool = False,
        source: str = "Last",
    ) -> tuple[list[IbkrHistoricalBar], str]:
        payload = self._request(
            "GET",
            "/iserver/marketdata/history",
            params={
                "conid": str(conid),
                "period": period,
                "bar": bar,
                "outsideRth": str(outside_rth).lower(),
                "source": source,
            },
        )
        if not isinstance(payload, dict):
            raise RuntimeError("IBKR returned an invalid historical market data response.")
        error = payload.get("error")
        if error:
            raise RuntimeError(f"IBKR historical data error: {error}")

        historical_bars: list[IbkrHistoricalBar] = []
        for item in payload.get("data", []):
            if not isinstance(item, dict):
                continue
            try:
                historical_bars.append(
                    IbkrHistoricalBar(
                        timestamp_ms=self._normalize_epoch_milliseconds(item.get("t")),
                        open=float(item["o"]),
                        high=float(item["h"]),
                        low=float(item["l"]),
                        close=float(item["c"]),
                        volume=self._optional_float(item.get("v")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        historical_bars.sort(key=lambda item: item.timestamp_ms)
        return historical_bars, str(payload.get("mdAvailability") or "Unknown")

    def preview_order(self, account_id: str, order: dict[str, Any]) -> Any:
        return self._request(
            "POST",
            f"/iserver/account/{account_id}/orders/whatif",
            json={"orders": [order]},
        )

    def place_order(self, account_id: str, order: dict[str, Any]) -> Any:
        with self._request_lock:
            payload = self._request(
                "POST",
                f"/iserver/account/{account_id}/orders",
                json={"orders": [order]},
            )
            self._pending_order_reply_id = self._extract_reply_id(payload)
            return payload

    def confirm_order_reply(self, reply_id: str, confirmed: bool) -> Any:
        with self._request_lock:
            if self._pending_order_reply_id != reply_id:
                raise RuntimeError("The IBKR order reply is no longer active.")
            payload = self._request(
                "POST",
                f"/iserver/reply/{reply_id}",
                json={"confirmed": confirmed},
                allow_during_order_reply=True,
            )
            self._pending_order_reply_id = self._extract_reply_id(payload) if confirmed else None
            return payload

    def get_live_orders(self) -> dict[str, Any]:
        payload = self._request("GET", "/iserver/account/orders")
        return payload if isinstance(payload, dict) else {"orders": []}

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/iserver/account/order/status/{order_id}")
        return payload if isinstance(payload, dict) else {}

    def cancel_order(self, account_id: str, order_id: str) -> dict[str, Any]:
        payload = self._request("DELETE", f"/iserver/account/{account_id}/order/{order_id}")
        return payload if isinstance(payload, dict) else {}

    def list_accounts(self) -> list[IbkrAccount]:
        payload = self._request("GET", "/portfolio/accounts")
        self._portfolio_accounts_primed = True
        accounts: list[IbkrAccount] = []
        for item in payload:
            account_id = str(item.get("accountId") or item.get("id") or "")
            if not account_id:
                continue
            accounts.append(
                IbkrAccount(
                    account_id=account_id,
                    display_name=str(item.get("displayName") or item.get("accountTitle") or account_id),
                    currency=str(item.get("currency") or ""),
                )
            )
        return accounts

    def resolved_account_id(self) -> str:
        if self.account_id:
            return self.account_id

        accounts = self.list_accounts()
        if not accounts:
            raise RuntimeError("IBKR did not return any accounts.")
        return accounts[0].account_id

    def get_positions(self, account_id: str | None = None) -> list[dict[str, Any]]:
        self._prime_portfolio_accounts()
        selected_account_id = account_id or self.resolved_account_id()
        return self._request("GET", f"/portfolio2/{selected_account_id}/positions")

    def get_summary(self, account_id: str | None = None) -> dict[str, Any]:
        self._prime_portfolio_accounts()
        selected_account_id = account_id or self.resolved_account_id()
        return self._request("GET", f"/portfolio/{selected_account_id}/summary")

    def format_status(self) -> str:
        try:
            status = self.auth_status()
        except Exception as exc:
            return f"IBKR gateway unreachable: {exc}"

        lines = [
            "IBKR gateway status:",
            f"- connected: {status.get('connected', False)}",
            f"- authenticated: {status.get('authenticated', False)}",
            f"- competing session: {status.get('competing', False)}",
        ]
        message = status.get("message")
        if message:
            lines.append(f"- message: {message}")
        if self.account_id:
            lines.append(f"- configured account: {self.account_id}")
        return "\n".join(lines)

    def format_accounts(self) -> str:
        accounts = self.list_accounts()
        if not accounts:
            return "IBKR returned no accounts."

        lines = ["IBKR accounts:"]
        for account in accounts:
            currency_suffix = f" ({account.currency})" if account.currency else ""
            lines.append(f"- {account.account_id}: {account.display_name}{currency_suffix}")
        return "\n".join(lines)

    def format_portfolio_snapshot(self, account_id: str | None = None) -> str:
        selected_account_id = account_id or self.resolved_account_id()
        summary = self.get_summary(selected_account_id)
        positions = self.get_positions(selected_account_id)

        lines = [f"IBKR portfolio snapshot for {selected_account_id}:"]
        for field, label in (
            ("netliquidation", "Net liquidation"),
            ("cashbalance", "Cash balance"),
            ("buyingpower", "Buying power"),
            ("maintmarginreq", "Maintenance margin"),
            ("equitywithloanvalue", "Equity with loan value"),
        ):
            value = self._extract_summary_value(summary, field)
            if value:
                lines.append(f"- {label}: {value}")

        if not positions:
            lines.append("- Holdings: none")
            return "\n".join(lines)

        ranked_positions = sorted(
            positions,
            key=lambda item: float(item.get("mktValue", 0) or 0),
            reverse=True,
        )
        lines.append("- Top positions:")
        for position in ranked_positions[:5]:
            symbol = position.get("contractDesc") or position.get("ticker") or position.get("conid") or "unknown"
            quantity = position.get("position", "0")
            market_value = position.get("mktValue", "unknown")
            market_price = position.get("mktPrice", "unknown")
            lines.append(
                f"  - {symbol}: qty={quantity}, market_price={market_price}, market_value={market_value}"
            )
        return "\n".join(lines)

    def _extract_summary_value(self, summary: dict[str, Any], key: str) -> str:
        raw_value = summary.get(key)
        if raw_value is None:
            return ""
        if isinstance(raw_value, dict):
            amount = raw_value.get("amount")
            currency = raw_value.get("currency")
            if amount is not None and currency:
                return f"{amount} {currency}"
            if amount is not None:
                return str(amount)
            return str(raw_value)
        return str(raw_value)

    def _prime_portfolio_accounts(self) -> None:
        if not self._portfolio_accounts_primed:
            self.list_accounts()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | list[Any] | None = None,
        allow_during_order_reply: bool = False,
    ) -> Any:
        with self._request_lock:
            if self._pending_order_reply_id and not allow_during_order_reply:
                raise IbkrPendingReplyError(
                    "An IBKR order warning is awaiting confirmation. "
                    "Use /confirm_reply or /cancel before making another broker request."
                )

            url = f"{self.base_url}{path}"
            try:
                with warnings.catch_warnings():
                    if not self.verify_ssl:
                        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
                    response = self.session.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json,
                        timeout=20,
                        verify=self.verify_ssl,
                    )
                response.raise_for_status()
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                raise IbkrApiError(method, path, status_code) from exc
            except requests.RequestException as exc:
                raise RuntimeError(f"{method} {url} failed: {exc}") from exc

            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(f"{method} {url} returned non-JSON content.") from exc

    def _extract_reply_id(self, payload: Any) -> str | None:
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            return None
        reply_id = payload[0].get("id")
        return str(reply_id) if reply_id else None

    @staticmethod
    def _snapshot_for_conid(snapshots: list[dict[str, Any]], conid: int) -> dict[str, Any]:
        for snapshot in snapshots:
            if str(snapshot.get("conid")) == str(conid):
                return snapshot
        return snapshots[0] if snapshots else {}

    @staticmethod
    def _has_quote_values(snapshot: dict[str, Any]) -> bool:
        return any(snapshot.get(field) not in (None, "") for field in ("31", "84", "86"))

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return None if value in (None, "") else str(value)

    @staticmethod
    def _market_data_mode(raw_value: Any) -> str:
        value = str(raw_value or "")
        modes = {
            "R": "RealTime",
            "D": "Delayed",
            "Z": "Frozen",
            "Y": "FrozenDelayed",
            "N": "NotSubscribed",
        }
        return modes.get(value[:1], "Unknown")

    @staticmethod
    def _format_market_timestamp(raw_value: Any) -> str | None:
        try:
            timestamp = float(raw_value) / 1000
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_epoch_milliseconds(value: Any) -> int:
        timestamp = int(value)
        if timestamp < 10_000_000_000:
            return timestamp * 1000
        if timestamp < 1_000_000_000_000:
            return timestamp * 100
        return timestamp
