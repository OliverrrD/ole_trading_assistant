"""Simple in-memory session store for Telegram user conversations."""

from __future__ import annotations

from app.model_gateway import ModelSession, OpenAIResponsesGateway


class InMemorySessionStore:
    def __init__(self, gateway: OpenAIResponsesGateway):
        self.gateway = gateway
        self._sessions: dict[str, ModelSession] = {}

    def get_or_create(self, user_id: str) -> ModelSession:
        session = self._sessions.get(user_id)
        if session is None:
            session = self.gateway.create_session(user_id)
            self._sessions[user_id] = session
        return session

    def reset(self, user_id: str) -> ModelSession:
        session = self.gateway.create_session(user_id)
        self._sessions[user_id] = session
        return session

    def get(self, user_id: str) -> ModelSession | None:
        return self._sessions.get(user_id)
