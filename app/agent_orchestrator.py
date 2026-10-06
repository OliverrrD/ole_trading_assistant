"""Bounded orchestration backed by a local in-process MCP server."""

from __future__ import annotations

from app.config import Settings
from app.ibkr_client import IbkrClient
from app.local_mcp import LocalTradingMcpSession
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
        mcp_session = LocalTradingMcpSession(
            user_id=user_id,
            settings=self.settings,
            ibkr_client=self.ibkr_client,
            market_data_service=self.market_data_service,
            market_store=self.market_store,
            trading_service=self.trading_service,
        )
        return self.gateway.send_user_message_with_tools(
            session=session,
            user_message=user_message,
            runtime_context=runtime_context,
            tools=mcp_session.openai_tools(),
            tool_executor=mcp_session.call_tool,
            max_rounds=4,
            max_total_calls=8,
        )
