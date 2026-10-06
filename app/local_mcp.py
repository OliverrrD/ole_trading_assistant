"""Local in-process MCP server and client adapter for trading tools."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal

from mcp import Client
from mcp.server import MCPServer
from mcp.types import TextContent, ToolAnnotations
from pydantic import Field

from app.config import Settings
from app.ibkr_client import IbkrClient
from app.market_data_service import MarketDataService
from app.market_store import MarketStore
from app.trading_service import PaperTradingService


class LocalTradingMcpSession:
    def __init__(
        self,
        user_id: str,
        settings: Settings,
        ibkr_client: IbkrClient,
        market_data_service: MarketDataService,
        market_store: MarketStore,
        trading_service: PaperTradingService,
    ):
        self.user_id = user_id
        self.settings = settings
        self.ibkr_client = ibkr_client
        self.market_data_service = market_data_service
        self.market_store = market_store
        self.trading_service = trading_service
        self.server = MCPServer(
            "Ole Trading Tools",
            instructions=(
                "Local tools scoped to the authenticated Telegram user. "
                "No tool can place, modify, confirm, or cancel a trade."
            ),
        )
        self._register_tools()

    def openai_tools(self) -> list[dict[str, Any]]:
        return asyncio.run(self._openai_tools())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = asyncio.run(self._call_tool(name, arguments))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        structured = result.structured_content
        if isinstance(structured, dict):
            nested = structured.get("result")
            if set(structured) == {"result"} and isinstance(nested, dict):
                return nested
            return structured

        text = "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return {"ok": not result.is_error, "result": text}
            if isinstance(decoded, dict):
                return decoded
        return {
            "ok": False,
            "error": "The local MCP tool returned no structured result.",
        }

    async def _openai_tools(self) -> list[dict[str, Any]]:
        async with Client(self.server, raise_exceptions=True) as client:
            result = await client.list_tools()
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description or tool.name,
                "parameters": tool.input_schema,
                "strict": False,
            }
            for tool in result.tools
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async with Client(self.server, raise_exceptions=True) as client:
            return await client.call_tool(name, arguments)

    def _register_tools(self) -> None:
        read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

        @self.server.tool(annotations=read_only)
        async def get_watchlist() -> dict[str, Any]:
            """Get the authenticated user's locally saved stock Watchlist."""
            items = self.market_store.list_watchlist(self.user_id)
            return {
                "ok": True,
                "watchlist": [
                    {
                        "symbol": item.symbol,
                        "company_name": item.company_name,
                        "exchange": item.listing_exchange,
                        "conid": item.conid,
                    }
                    for item in items
                ],
            }

        @self.server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=True,
            )
        )
        async def add_to_watchlist(symbol: str) -> dict[str, Any]:
            """Add or refresh a US stock or ETF in the user's Watchlist when explicitly requested."""
            if self.trading_service.has_pending_broker_reply:
                return {"ok": False, "error": self.trading_service.pending_reply_message()}
            return self._text_result(
                self.market_data_service.add_watch(self.user_id, symbol)
            )

        @self.server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=True,
                open_world_hint=False,
            )
        )
        async def remove_from_watchlist(symbol: str) -> dict[str, Any]:
            """Remove a stock or ETF from the user's Watchlist when explicitly requested."""
            return self._text_result(
                self.market_data_service.remove_watch(self.user_id, symbol)
            )

        @self.server.tool(annotations=read_only)
        async def get_quote(symbol: str) -> dict[str, Any]:
            """Fetch the latest available IBKR quote and data mode for a symbol."""
            return self._text_result(self.market_data_service.format_quote(symbol))

        @self.server.tool(annotations=read_only)
        async def get_technical_snapshot(
            symbol: str,
            timeframe: Literal["1m", "10m", "1h", "1d"],
        ) -> dict[str, Any]:
            """Refresh and return deterministic technical indicators for a symbol and timeframe."""
            return self._text_result(
                self.market_data_service.format_technical(symbol, timeframe)
            )

        @self.server.tool(annotations=read_only)
        async def get_price_history(
            symbol: str,
            period: Literal["1m", "3m", "6m", "1y"] = "1y",
        ) -> dict[str, Any]:
            """Return stored daily OHLCV bars and range statistics for the requested period."""
            return self.market_data_service.price_history(symbol, period)

        @self.server.tool(annotations=read_only)
        async def get_portfolio() -> dict[str, Any]:
            """Get the user's current IBKR portfolio when account context is enabled."""
            if not self.settings.ibkr_include_account_context:
                return {
                    "ok": False,
                    "error": "Portfolio access is disabled by configuration.",
                }
            if self.trading_service.has_pending_broker_reply:
                return {"ok": False, "error": self.trading_service.pending_reply_message()}
            try:
                status = self.ibkr_client.auth_status()
                if not status.get("connected") or not status.get("authenticated"):
                    return {
                        "ok": False,
                        "error": (
                            "IBKR brokerage session is not authenticated. "
                            "Open https://localhost:5000, log in again, then retry."
                        ),
                    }
                return self._text_result(self.ibkr_client.format_portfolio_snapshot())
            except Exception as exc:
                return self._tool_error("IBKR portfolio", exc)

        @self.server.tool(annotations=read_only)
        async def get_active_orders() -> dict[str, Any]:
            """Read paper-account order status without changing or submitting any order."""
            return self._text_result(self.trading_service.list_orders())

        @self.server.tool(annotations=read_only)
        async def search_stock_memory(
            symbol: str,
            limit: Annotated[int, Field(ge=1, le=10)] = 5,
        ) -> dict[str, Any]:
            """Retrieve prior conversations, decisions, risks, and analysis for a watched symbol."""
            contract = self.market_store.find_instrument(symbol.upper())
            if contract is None:
                return {
                    "ok": False,
                    "error": f"No known instrument for {symbol.upper()}.",
                }
            observations = self.market_store.recent_symbol_observations(
                self.user_id,
                contract.conid,
                limit,
            )
            return {
                "ok": True,
                "symbol": contract.symbol,
                "warning": "Historical opinions and decisions are not current market facts.",
                "memories": [
                    {
                        "created_at": item.created_at,
                        "memory_type": item.memory_type,
                        "relation_type": item.relation_type,
                        "user_message": item.user_message,
                        "assistant_response": item.assistant_response,
                    }
                    for item in observations
                ],
            }

    @staticmethod
    def _tool_error(operation: str, exc: Exception) -> dict[str, Any]:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            detail = (
                "IBKR session is unauthorized. Open https://localhost:5000, "
                "log in again, then retry."
            )
        elif status_code:
            detail = f"IBKR returned HTTP {status_code}."
        else:
            detail = f"{type(exc).__name__}."
        return {"ok": False, "error": f"{operation} unavailable: {detail}"}

    @staticmethod
    def _text_result(message: str) -> dict[str, Any]:
        failure_prefixes = (
            "Quote lookup failed:",
            "Technical analysis failed:",
            "Order query failed:",
            "Could not add watch:",
            "Could not remove watch:",
            "No stored",
        )
        return {
            "ok": not message.startswith(failure_prefixes),
            "result": message,
        }
