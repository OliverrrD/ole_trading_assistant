"""Bounded read-only tool orchestration for natural-language conversations."""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.ibkr_client import IbkrClient
from app.market_data_service import MarketDataService
from app.market_store import MarketStore
from app.model_gateway import ModelResponse, ModelSession, OpenAIResponsesGateway
from app.trading_service import PaperTradingService


class AgentOrchestrator:
    def __init__(
        self,
        settings: Settings,
        gateway: OpenAIResponsesGateway,
        ibkr_client: IbkrClient,
        market_data_service: MarketDataService,
        market_store: MarketStore,
        trading_service: PaperTradingService,
    ):
        self.settings = settings
        self.gateway = gateway
        self.ibkr_client = ibkr_client
        self.market_data_service = market_data_service
        self.market_store = market_store
        self.trading_service = trading_service

    def run(
        self,
        session: ModelSession,
        user_id: str,
        user_message: str,
        runtime_context: str,
    ) -> ModelResponse:
        return self.gateway.send_user_message_with_tools(
            session=session,
            user_message=user_message,
            runtime_context=runtime_context,
            tools=self._tool_definitions(),
            tool_executor=lambda name, arguments: self._execute(
                user_id,
                name,
                arguments,
            ),
            max_rounds=4,
            max_total_calls=8,
        )

    def _execute(
        self,
        user_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "get_watchlist":
            items = self.market_store.list_watchlist(user_id)
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
        if name == "get_quote":
            return self._text_result(
                self.market_data_service.format_quote(self._required_string(arguments, "symbol"))
            )
        if name == "get_technical_snapshot":
            return self._text_result(
                self.market_data_service.format_technical(
                    self._required_string(arguments, "symbol"),
                    self._required_string(arguments, "timeframe"),
                )
            )
        if name == "get_portfolio":
            if not self.settings.ibkr_include_account_context:
                return {"ok": False, "error": "Portfolio access is disabled by configuration."}
            if self.trading_service.has_pending_broker_reply:
                return {"ok": False, "error": self.trading_service.pending_reply_message()}
            return self._text_result(self.ibkr_client.format_portfolio_snapshot())
        if name == "get_active_orders":
            return self._text_result(self.trading_service.list_orders())
        if name == "search_stock_memory":
            return self._search_stock_memory(
                user_id,
                self._required_string(arguments, "symbol"),
                arguments.get("limit", 5),
            )
        return {"ok": False, "error": f"Unknown tool: {name}"}

    def _search_stock_memory(
        self,
        user_id: str,
        symbol: str,
        raw_limit: Any,
    ) -> dict[str, Any]:
        contract = self.market_store.find_instrument(symbol.upper())
        if contract is None:
            return {"ok": False, "error": f"No known instrument for {symbol.upper()}."}
        try:
            limit = min(max(int(raw_limit), 1), 10)
        except (TypeError, ValueError):
            limit = 5
        observations = self.market_store.recent_symbol_observations(
            user_id,
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
    def _required_string(arguments: dict[str, Any], key: str) -> str:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _text_result(message: str) -> dict[str, Any]:
        failure_prefixes = (
            "Quote lookup failed:",
            "Technical analysis failed:",
            "Order query failed:",
            "No stored",
        )
        success = not message.startswith(failure_prefixes)
        return {"ok": success, "result": message}

    @staticmethod
    def _tool_definitions() -> list[dict[str, Any]]:
        no_arguments = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        symbol_argument = {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "US stock or ETF ticker, for example AAPL.",
                }
            },
            "required": ["symbol"],
            "additionalProperties": False,
        }
        return [
            {
                "type": "function",
                "name": "get_watchlist",
                "description": "Get the current Telegram user's locally saved stock Watchlist.",
                "parameters": no_arguments,
                "strict": True,
            },
            {
                "type": "function",
                "name": "get_quote",
                "description": "Fetch the latest available IBKR quote and data mode for a symbol. Use for current price questions.",
                "parameters": symbol_argument,
                "strict": True,
            },
            {
                "type": "function",
                "name": "get_technical_snapshot",
                "description": "Refresh and return deterministic stored technical indicators for a watched symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "timeframe": {
                            "type": "string",
                            "enum": ["1m", "10m", "1h", "1d"],
                        },
                    },
                    "required": ["symbol", "timeframe"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
            {
                "type": "function",
                "name": "get_portfolio",
                "description": "Get the user's current IBKR portfolio summary and holdings when account context is enabled.",
                "parameters": no_arguments,
                "strict": True,
            },
            {
                "type": "function",
                "name": "get_active_orders",
                "description": "Read current paper-account order status. This tool cannot place, modify, confirm, or cancel orders.",
                "parameters": no_arguments,
                "strict": True,
            },
            {
                "type": "function",
                "name": "search_stock_memory",
                "description": "Retrieve the user's prior conversations, decisions, risks, and analysis indexed to a watched symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["symbol", "limit"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        ]
