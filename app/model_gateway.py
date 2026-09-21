"""OpenAI Responses API gateway for the assistant's foundational model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI

DEFAULT_SYSTEM_PROMPT = """
You are Ole Trading Assistant, a personal US stock trading assistant.

Rules:
- Be concise, practical, and explicit about uncertainty.
- Never claim a live trade was executed unless the broker confirms it.
- Never instruct the system to submit a live trade without explicit user approval.
- If account or market data is missing, say so clearly.
- When discussing trading ideas, distinguish analysis from execution.
""".strip()

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass
class ModelSession:
    user_id: str
    model: str
    previous_response_id: str | None = None
    turn_count: int = 0


@dataclass
class ModelResponse:
    text: str
    response_id: str | None
    tool_executions: tuple["ToolExecution", ...] = ()


@dataclass(frozen=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    success: bool


class OpenAIResponsesGateway:
    def __init__(
        self,
        api_key: str,
        model: str,
        organization: str = "",
        project: str = "",
        base_url: str = "",
        reasoning_effort: str = "",
        text_verbosity: str = "low",
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ):
        self.base_url = base_url or DEFAULT_OPENAI_BASE_URL
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if organization:
            client_kwargs["organization"] = organization
        if project:
            client_kwargs["project"] = project
        client_kwargs["base_url"] = self.base_url

        self.client = OpenAI(**client_kwargs)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.text_verbosity = text_verbosity
        self.system_prompt = system_prompt

    def create_session(self, user_id: str) -> ModelSession:
        return ModelSession(user_id=user_id, model=self.model)

    def validate_credentials(self, secret: str) -> bool:
        probe_client = OpenAI(api_key=secret, base_url=self.base_url)
        try:
            response = probe_client.responses.create(
                model=self.model,
                input="Reply with OK.",
                max_output_tokens=8,
                text={"verbosity": "low"},
            )
            return bool(self._extract_output_text(response))
        except Exception:
            return False

    def send_user_message(
        self,
        session: ModelSession,
        user_message: str,
        runtime_context: str = "",
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": session.model,
            "instructions": self.system_prompt,
            "input": self._build_input_text(user_message, runtime_context),
            "text": {"verbosity": self.text_verbosity},
        }
        if self.reasoning_effort:
            request["reasoning"] = {"effort": self.reasoning_effort}
        if session.previous_response_id:
            request["previous_response_id"] = session.previous_response_id

        response = self.client.responses.create(**request)
        response_id = getattr(response, "id", None)
        session.previous_response_id = response_id
        session.turn_count += 1

        return ModelResponse(
            text=self._extract_output_text(response) or "I could not generate a reply.",
            response_id=response_id,
        )

    def send_user_message_with_tools(
        self,
        session: ModelSession,
        user_message: str,
        runtime_context: str,
        tools: list[dict[str, Any]],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
        *,
        max_rounds: int = 4,
        max_total_calls: int = 8,
    ) -> ModelResponse:
        input_items: Any = self._build_input_text(user_message, runtime_context)
        previous_response_id = session.previous_response_id
        executions: list[ToolExecution] = []

        for _ in range(max_rounds):
            response = self.client.responses.create(
                **self._tool_request(
                    session,
                    input_items,
                    previous_response_id,
                    tools,
                )
            )
            response_id = getattr(response, "id", None)
            calls = self._extract_function_calls(response)
            if not calls:
                session.previous_response_id = response_id
                session.turn_count += 1
                return ModelResponse(
                    text=self._extract_output_text(response) or "I could not generate a reply.",
                    response_id=response_id,
                    tool_executions=tuple(executions),
                )

            previous_response_id = response_id
            input_items = []
            for call in calls:
                if len(executions) >= max_total_calls:
                    result = {"ok": False, "error": "Tool call limit reached."}
                    arguments: dict[str, Any] = {}
                    success = False
                    executions.append(ToolExecution(call["name"], arguments, success))
                else:
                    arguments, argument_error = self._parse_tool_arguments(call["arguments"])
                    if argument_error:
                        result = {"ok": False, "error": argument_error}
                        success = False
                    else:
                        try:
                            result = tool_executor(call["name"], arguments)
                            success = bool(result.get("ok", True))
                        except Exception as exc:
                            result = {"ok": False, "error": str(exc)}
                            success = False
                    executions.append(ToolExecution(call["name"], arguments, success))
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

        final_request = self._base_request(session)
        final_request.update(
            {
                "input": input_items,
                "tools": tools,
                "tool_choice": "none",
            }
        )
        if previous_response_id:
            final_request["previous_response_id"] = previous_response_id
        response = self.client.responses.create(**final_request)
        response_id = getattr(response, "id", None)
        session.previous_response_id = response_id
        session.turn_count += 1
        return ModelResponse(
            text=self._extract_output_text(response) or "I could not generate a reply.",
            response_id=response_id,
            tool_executions=tuple(executions),
        )

    def _tool_request(
        self,
        session: ModelSession,
        input_items: Any,
        previous_response_id: str | None,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request = self._base_request(session)
        request.update(
            {
                "input": input_items,
                "tools": tools,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }
        )
        if previous_response_id:
            request["previous_response_id"] = previous_response_id
        return request

    def _base_request(self, session: ModelSession) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": session.model,
            "instructions": self.system_prompt,
            "text": {"verbosity": self.text_verbosity},
        }
        if self.reasoning_effort:
            request["reasoning"] = {"effort": self.reasoning_effort}
        return request

    @staticmethod
    def _extract_function_calls(response: Any) -> list[dict[str, str]]:
        calls: list[dict[str, str]] = []
        for item in getattr(response, "output", None) or []:
            item_type = getattr(item, "type", None)
            if item_type is None and isinstance(item, dict):
                item_type = item.get("type")
            if item_type != "function_call":
                continue
            name = getattr(item, "name", None)
            arguments = getattr(item, "arguments", None)
            call_id = getattr(item, "call_id", None)
            if isinstance(item, dict):
                name = name or item.get("name")
                arguments = arguments or item.get("arguments")
                call_id = call_id or item.get("call_id")
            if name and call_id:
                calls.append(
                    {
                        "name": str(name),
                        "arguments": str(arguments or "{}"),
                        "call_id": str(call_id),
                    }
                )
        return calls

    @staticmethod
    def _parse_tool_arguments(raw_arguments: str) -> tuple[dict[str, Any], str | None]:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}, "Tool arguments were not valid JSON."
        if not isinstance(arguments, dict):
            return {}, "Tool arguments must be a JSON object."
        return arguments, None

    def _build_input_text(self, user_message: str, runtime_context: str) -> str:
        parts = []
        if runtime_context:
            parts.append("Runtime context:\n" + runtime_context.strip())
        parts.append("User request:\n" + user_message.strip())
        return "\n\n".join(parts)

    def _extract_output_text(self, response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output_items = getattr(response, "output", None) or []
        extracted: list[str] = []
        for item in output_items:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content", [])
            for part in content or []:
                text = getattr(part, "text", None)
                if text is None and isinstance(part, dict):
                    text = part.get("text")
                if text:
                    extracted.append(str(text).strip())

        return "\n".join(piece for piece in extracted if piece)
