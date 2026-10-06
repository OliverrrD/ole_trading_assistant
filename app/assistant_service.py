"""High-level orchestration for setup, portfolio reads, and model chat."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent_orchestrator import AgentOrchestrator
from app.config import Settings
from app.ibkr_client import IbkrClient, IbkrContract
from app.market_data_service import MarketDataService
from app.market_monitor import MarketDataMonitor
from app.market_store import MarketStore
from app.model_gateway import OpenAIResponsesGateway
from app.session_store import InMemorySessionStore
from app.trading_service import PaperTradingService


@dataclass(frozen=True)
class SetupItem:
    label: str
    configured: bool
    detail: str


class TradingAssistantService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ibkr_client = IbkrClient(
            base_url=settings.ibkr_base_url,
            verify_ssl=settings.ibkr_verify_ssl,
            account_id=settings.ibkr_account_id,
        )
        self.trading_service = PaperTradingService(settings, self.ibkr_client)
        self.market_store = MarketStore(settings.market_database_path)
        self.market_data_service = MarketDataService(self.ibkr_client, self.market_store)
        self.market_monitor = MarketDataMonitor(
            self.market_data_service,
            settings.market_sync_interval_seconds,
        )
        self._stock_memory_indexed_users: set[str] = set()
        self.gateway = self._build_gateway()
        self.session_store = InMemorySessionStore(self.gateway) if self.gateway else None
        self.orchestrator = (
            AgentOrchestrator(
                settings,
                self.gateway,
                self.ibkr_client,
                self.market_data_service,
                self.market_store,
                self.trading_service,
            )
            if self.gateway
            else None
        )

    def _build_gateway(self) -> OpenAIResponsesGateway | None:
        if not self.settings.has_openai_config:
            return None
        return OpenAIResponsesGateway(
            api_key=self.settings.openai_api_key,
            model=self.settings.openai_model,
            organization=self.settings.openai_org_id,
            project=self.settings.openai_project_id,
            base_url=self.settings.openai_base_url,
            reasoning_effort=self.settings.openai_reasoning_effort,
            text_verbosity=self.settings.openai_text_verbosity,
            system_prompt=self._system_prompt(),
        )

    def _system_prompt(self) -> str:
        return (
            f"You are {self.settings.assistant_name}, a personal US stock trading assistant.\n"
            "Understand requests in any language and reply in the user's language unless they "
            "explicitly request another language.\n"
            "Use the available tools whenever the user asks for current, dynamic, "
            "account-specific, or locally stored information.\n"
            "When the user clearly asks to add or remove a symbol from the Watchlist, use the "
            "corresponding Watchlist tool instead of asking them to type a command.\n"
            "Never invent quotes, holdings, order status, technical indicators, watchlists, "
            "or saved conversation history.\n"
            "Tool results can be delayed or stale; preserve any timestamps and data-mode warnings.\n"
            "You can explain portfolio context, discuss trading ideas, and draft trade requests.\n"
            "You must never claim a real trade has been submitted unless a broker tool confirms it.\n"
            "You must never ask the system to execute a trade without explicit user approval.\n"
            "No execution tools are available in chat. Direct trade requests to the explicit "
            "/order command, which still requires /confirm.\n"
            "When a message expresses a stock-specific risk, decision, trade idea, thesis, or "
            "outcome, call classify_stock_memory once based on its meaning, regardless of language.\n"
            "Keep replies concise and practical."
        )

    def setup_items(self) -> list[SetupItem]:
        return [
            SetupItem("Telegram bot token", self.settings.has_telegram_config, "Required to run the bot."),
            SetupItem(
                "Telegram user allowlist",
                bool(self.settings.telegram_allowed_user_ids),
                f"{len(self.settings.telegram_allowed_user_ids)} allowed user(s)",
            ),
            SetupItem("OpenAI API key", self.settings.has_openai_config, "Required for chat and model reasoning."),
            SetupItem("OpenAI model", True, self.settings.openai_model),
            SetupItem("IBKR gateway URL", self.settings.has_ibkr_config, self.settings.ibkr_base_url),
            SetupItem(
                "IBKR account id",
                bool(self.settings.ibkr_account_id),
                "Optional. Leave blank to auto-select the first available account.",
            ),
            SetupItem(
                "IBKR include context",
                True,
                str(self.settings.ibkr_include_account_context).lower(),
            ),
            SetupItem(
                "IBKR trading mode",
                self.settings.ibkr_trading_mode == "paper",
                self.settings.ibkr_trading_mode,
            ),
            SetupItem(
                "Market bar sync",
                self.settings.market_sync_enabled,
                f"every {max(60, self.settings.market_sync_interval_seconds)} seconds",
            ),
        ]

    def format_setup_message(self) -> str:
        lines = ["Setup checklist:"]
        for item in self.setup_items():
            status = "configured" if item.configured else "missing"
            lines.append(f"- {item.label}: {status} ({item.detail})")
        lines.append("")
        lines.append("Put secrets in `.env`, restart the bot, then use `/model_status` or send a message.")
        return "\n".join(lines)

    def format_model_status(self, user_id: str) -> str:
        if not self.gateway or not self.session_store:
            return (
                "Model gateway is not configured.\n"
                "Set `OPENAI_API_KEY` in `.env`, optionally adjust `OPENAI_MODEL`, and restart the bot."
            )

        session = self.session_store.get(user_id)
        if session is None:
            return (
                f"Model gateway is ready.\n"
                f"- model: {self.settings.openai_model}\n"
                "- session: not started yet"
            )

        return (
            f"Model gateway is ready.\n"
            f"- model: {session.model}\n"
            f"- turns in current chat: {session.turn_count}\n"
            f"- last response id: {session.previous_response_id or 'none'}"
        )

    def reset_chat(self, user_id: str) -> str:
        if not self.session_store:
            return "Model gateway is not configured yet. Use `/setup` to see missing values."
        session = self.session_store.reset(user_id)
        return f"Started a fresh chat session with model `{session.model}`."

    def assistant_status(self, user_id: str) -> str:
        session = self.session_store.get(user_id) if self.session_store else None
        lines = [
            "Assistant status:",
            f"- Telegram configured: {'yes' if self.settings.has_telegram_config else 'no'}",
            f"- OpenAI configured: {'yes' if self.settings.has_openai_config else 'no'}",
            f"- Model: {self.settings.openai_model}",
            f"- IBKR gateway: {self.settings.ibkr_base_url}",
            f"- IBKR trading mode: {self.settings.ibkr_trading_mode}",
            f"- Market sync enabled: {str(self.settings.market_sync_enabled).lower()}",
            f"- Orchestrator: {'enabled' if self.orchestrator else 'disabled'}",
            f"- Active chat turns: {session.turn_count if session else 0}",
        ]
        return "\n".join(lines)

    def ibkr_status(self) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.ibkr_client.format_status()

    def ibkr_accounts(self) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        try:
            return self.ibkr_client.format_accounts()
        except Exception as exc:
            return self._ibkr_read_error("account list", exc)

    def ibkr_portfolio(self) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        try:
            return self.ibkr_client.format_portfolio_snapshot()
        except Exception as exc:
            return self._ibkr_read_error("portfolio", exc)

    @staticmethod
    def _ibkr_read_error(operation: str, exc: Exception) -> str:
        status_code = getattr(exc, "status_code", None)
        if status_code == 401:
            return (
                f"IBKR {operation} unavailable: the Gateway session is unauthorized. "
                "Open https://localhost:5000, log in again, then retry."
            )
        if status_code:
            return f"IBKR {operation} unavailable: HTTP {status_code}."
        return f"IBKR {operation} unavailable: {type(exc).__name__}."

    def create_order(self, user_id: str, arguments: list[str]) -> str:
        return self.trading_service.create_order(user_id, arguments)

    def confirm_order(self, user_id: str, token: str) -> str:
        return self.trading_service.confirm_order(user_id, token)

    def confirm_order_reply(self, user_id: str, token: str) -> str:
        return self.trading_service.confirm_broker_reply(user_id, token)

    def cancel_pending_order(self, user_id: str, token: str) -> str:
        return self.trading_service.cancel_pending(user_id, token)

    def list_orders(self) -> str:
        return self.trading_service.list_orders()

    def order_status(self, order_id: str) -> str:
        return self.trading_service.order_status(order_id)

    def cancel_live_order(self, order_id: str) -> str:
        return self.trading_service.cancel_live_order(order_id)

    def add_watch(self, user_id: str, symbol: str) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.market_data_service.add_watch(user_id, symbol)

    def remove_watch(self, user_id: str, symbol: str) -> str:
        return self.market_data_service.remove_watch(user_id, symbol)

    def watchlist(self, user_id: str) -> str:
        return self.market_data_service.format_watchlist(user_id)

    def quote(self, symbol: str) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.market_data_service.format_quote(symbol)

    def technical(self, symbol: str, timeframe: str) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.market_data_service.format_technical(symbol, timeframe)

    def price_history(self, symbol: str, period: str) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.market_data_service.format_price_history(symbol, period)

    def market_sync(self) -> str:
        if self.trading_service.has_pending_broker_reply:
            return self.trading_service.pending_reply_message()
        return self.market_data_service.format_market_sync()

    def market_monitor_status(self) -> str:
        return self.market_monitor.format_status()

    def start_market_monitor(self) -> None:
        if self.settings.market_sync_enabled:
            self.market_monitor.start()

    def stop_market_monitor(self) -> None:
        self.market_monitor.stop()

    def conversation_history(self, user_id: str, symbol: str = "") -> str:
        self._ensure_stock_memory_index(user_id)
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol:
            contract = self.market_store.find_instrument(normalized_symbol)
            if contract is None:
                return f"No known instrument for {normalized_symbol}. Use /watch {normalized_symbol} first."
            observations = self.market_store.recent_symbol_observations(
                user_id,
                contract.conid,
                limit=5,
            )
            if not observations:
                return f"No saved conversations are indexed to {normalized_symbol}."
            lines = [f"Recent saved conversations for {normalized_symbol}:"]
            for observation in observations:
                lines.extend(
                    [
                        f"- {observation.created_at} "
                        f"[{observation.memory_type}/{observation.relation_type}]",
                        f"  You: {self._compact_text(observation.user_message, 240)}",
                        f"  {self.settings.assistant_name}: "
                        f"{self._compact_text(observation.assistant_response, 320)}",
                    ]
                )
            return "\n".join(lines)

        observations = self.market_store.recent_observations(user_id, limit=5)
        if not observations:
            return "No saved conversation history."
        lines = ["Recent saved conversations:"]
        for observation in observations:
            lines.extend(
                [
                    f"- {observation.created_at}",
                    f"  You: {self._compact_text(observation.user_message, 240)}",
                    f"  {self.settings.assistant_name}: "
                    f"{self._compact_text(observation.assistant_response, 320)}",
                ]
            )
        return "\n".join(lines)

    def clear_conversation_history(self, user_id: str, confirmation: str) -> str:
        if confirmation != "CONFIRM":
            return "Usage: /clear_history CONFIRM"
        deleted = self.market_store.clear_observations(user_id)
        if self.session_store:
            self.session_store.reset(user_id)
        return (
            f"Deleted {deleted} saved conversation(s), cleared their tool audit history, "
            "and reset the active chat session."
        )

    def chat(self, user_id: str, message: str) -> str:
        if not self.gateway or not self.session_store or not self.orchestrator:
            return (
                "Model gateway is not configured yet.\n"
                "Set `OPENAI_API_KEY` in `.env`, restart the bot, and use `/setup` to verify."
            )

        self._ensure_stock_memory_index(user_id)
        existing_session = self.session_store.get(user_id)
        session = self.session_store.get_or_create(user_id)
        runtime_context = self._runtime_context(user_id)
        linked_contracts = self.market_data_service.extract_user_instruments(user_id, message)
        if linked_contracts:
            symbol_history = self._symbol_history_context(user_id, linked_contracts)
            if symbol_history:
                runtime_context = f"{runtime_context}\n\n{symbol_history}"
        elif existing_session is None:
            restored_history = self._history_context(user_id)
            if restored_history:
                runtime_context = f"{runtime_context}\n\n{restored_history}"
        response = self.orchestrator.run(
            session=session,
            user_id=user_id,
            user_message=message,
            runtime_context=runtime_context,
        )
        sensitive_tools = {"get_portfolio", "get_active_orders"}
        stored_response = (
            "[Broker account response omitted from local conversation history.]"
            if any(execution.name in sensitive_tools for execution in response.tool_executions)
            else response.text
        )
        self.market_store.save_observation(
            user_id=user_id,
            user_message=message,
            assistant_response=stored_response,
            watchlist_symbols=self.market_data_service.context_summary(user_id),
            runtime_context=runtime_context,
            model=session.model,
            response_id=response.response_id,
            max_entries=self.settings.agent_history_max_entries,
            symbol_conids=tuple(contract.conid for contract in linked_contracts),
            memory_type=self._memory_type_from_response(response),
        )
        self.market_store.save_tool_executions(
            user_id=user_id,
            response_id=response.response_id,
            executions=response.tool_executions,
            max_conversations=self.settings.agent_history_max_entries,
        )
        return response.text

    def _symbol_history_context(self, user_id: str, contracts: list[IbkrContract]) -> str:
        lines = ["Relevant stock-specific local memory:"]
        seen_observations: set[int] = set()
        found = False
        for contract in contracts:
            observations = self.market_store.recent_symbol_observations(
                user_id,
                contract.conid,
                limit=5,
            )
            for observation in observations:
                if observation.observation_id in seen_observations:
                    continue
                seen_observations.add(observation.observation_id)
                found = True
                lines.append(
                    f"- {observation.created_at} [{observation.symbol}] "
                    f"[{observation.memory_type}]: "
                    f"User: {self._compact_text(observation.user_message, 500)} | "
                    f"Assistant: {self._compact_text(observation.assistant_response, 700)}"
                )
        if not found:
            return ""
        lines.append(
            "These are historical opinions or decisions, not current market facts. "
            "Prefer current technical, portfolio, and order data when they conflict."
        )
        return "\n".join(lines)

    def _ensure_stock_memory_index(self, user_id: str) -> None:
        if user_id in self._stock_memory_indexed_users:
            return
        for observation in self.market_store.observations_without_symbol_index(user_id):
            contracts = self.market_data_service.extract_user_instruments(
                user_id,
                observation.user_message,
            )
            if not contracts:
                continue
            self.market_store.link_observation_symbols(
                observation.observation_id,
                tuple(contract.conid for contract in contracts),
                "analysis",
                observation.created_at,
            )
        self._stock_memory_indexed_users.add(user_id)

    @staticmethod
    def _memory_type_from_response(response: object) -> str:
        allowed_types = {"risk", "decision", "trade_idea", "thesis", "outcome"}
        for execution in getattr(response, "tool_executions", ()):
            if getattr(execution, "name", "") != "classify_stock_memory":
                continue
            arguments = getattr(execution, "arguments", {})
            memory_type = arguments.get("memory_type") if isinstance(arguments, dict) else None
            if memory_type in allowed_types:
                return memory_type
        return "analysis"

    def _history_context(self, user_id: str) -> str:
        observations = self.market_store.recent_observations(user_id, limit=3)
        if not observations:
            return ""
        lines = ["Recent local conversation history restored after restart:"]
        for observation in observations:
            lines.append(f"User: {self._compact_text(observation.user_message, 600)}")
            lines.append(
                f"Assistant: {self._compact_text(observation.assistant_response, 800)}"
            )
        lines.append("Treat this history as context, not as current market data.")
        return "\n".join(lines)

    @staticmethod
    def _compact_text(value: str, limit: int) -> str:
        compact = " ".join(value.split())
        return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"

    def _runtime_context(self, user_id: str) -> str:
        lines = [
            f"Assistant name: {self.settings.assistant_name}",
            f"Model: {self.settings.openai_model}",
            f"User watchlist: {self.market_data_service.context_summary(user_id)}",
            "Stored daily technical state:",
            self.market_data_service.technical_context_summary(user_id),
        ]
        lines.append(
            "IBKR portfolio read tool: "
            + ("available on demand" if self.settings.ibkr_include_account_context else "disabled")
        )
        return "\n".join(lines)
