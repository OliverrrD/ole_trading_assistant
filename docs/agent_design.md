# Ole Trading Assistant

## Goal
A personal US stock trading assistant with human approval for all real trading actions.

## Missing foundational layer
The current design defines the broker side and approval side, but it does not yet define the foundational model entrypoint:
- how the assistant talks to the model
- how a user binds OpenAI credentials to the assistant
- how chat sessions are persisted across Telegram turns
- how model tool calls are constrained before any trade action

This layer should be treated as a first-class subsystem, not as a helper inside the Telegram bot.

## Foundational model choice
Use OpenAI `Responses API` as the primary model interface.

Recommended default model:
- `gpt-5-codex` for coding-style agent behavior and tool-oriented orchestration

Optional override:
- allow model selection through config so the system can later swap to `gpt-5.2-codex`, `gpt-5.2`, or another approved OpenAI model without changing Telegram or broker logic

Rationale:
- Responses API supports multi-turn conversation state
- Responses API is the natural surface for tool calls and agent workflows
- `gpt-5-codex` is optimized for agentic coding-style tasks and works well as a foundational reasoning model for a trading assistant controller

## Model access and login
Do not design this as "log into ChatGPT inside Telegram."

Preferred pattern:
- the trading assistant owns the OpenAI API integration on the server side
- the user completes a one-time model binding flow
- the assistant stores only server-side credentials or a reference to them

The login UX should be:
1. User sends `/connect_model` in Telegram
2. Bot replies with a short-lived secure link or one-time pairing code
3. User opens the local admin page and chooses `OpenAI`
4. User pastes an OpenAI project API key or selects a preconfigured server credential
5. Server validates the credential with a lightweight OpenAI request
6. Server stores the credential in a secure store and marks the Telegram user as `model_connected`
7. Bot confirms connection and starts a fresh model session

This gives the user a "login" experience without exposing API secrets inside Telegram message history.

## Core subsystems
### 1. Surface agent
- `TelegramBot`
- receives user commands and free-form chat
- never calls Robinhood directly
- always goes through the orchestration layer

### 2. Session manager
- maps `telegram_user_id -> assistant_session`
- stores:
  - active provider
  - approved model
  - OpenAI conversation id
  - recent turns summary
  - portfolio context snapshot id
  - pending approval request id

### 3. Model gateway
- a provider-neutral interface used by the rest of the app
- first implementation: `OpenAIResponsesGateway`
- future providers can be added behind the same contract

### 4. Credential store
- stores encrypted provider credentials or references to env-backed credentials
- never returns raw secrets to Telegram
- supports revoke, rotate, and validate flows

### 5. Tool orchestration layer
- exposes only approved tools to the model
- examples:
  - portfolio summary read
  - quote lookup
  - research lookup
  - draft trade request
- does not expose direct trade execution as a free tool in v1

### 6. Approval boundary
- model may propose a trade
- approval gate transforms proposal into a human-readable approval request
- only after explicit user approval can the execution service submit the order

## Interface design
Suggested Python contract:

```python
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Role = Literal["system", "developer", "user", "assistant", "tool"]


@dataclass
class ModelTurn:
    role: Role
    content: str


@dataclass
class ModelSession:
    session_id: str
    provider: str
    model: str
    external_conversation_id: str | None = None


@dataclass
class ModelResult:
    reply_text: str
    tool_calls: list[dict[str, Any]]
    raw_response_id: str


class FoundationModelGateway(Protocol):
    def create_session(self, user_id: str, model: str) -> ModelSession: ...
    def send_turn(
        self,
        session: ModelSession,
        turns: list[ModelTurn],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResult: ...
    def close_session(self, session: ModelSession) -> None: ...
    def validate_credentials(self, secret: str) -> bool: ...
```

OpenAI-backed implementation:
- `OpenAIResponsesGateway.create_session()` creates local session metadata
- `send_turn()` calls OpenAI Responses API and persists the returned conversation id or previous response id
- `validate_credentials()` performs a small authenticated request before saving the key

## Prompt architecture
Use a layered prompt contract:

### System prompt
Defines the assistant identity and hard safety rules:
- US stock trading assistant
- never execute real trades without explicit user approval
- explain reasoning in plain English
- ask for missing details before proposing an order

### Developer prompt
Defines runtime behavior:
- available tools
- approval policy
- formatting rules for trade proposals
- when to summarize portfolio state
- when to refuse or defer

### User prompt
Telegram user message, for example:
- "summarize my portfolio"
- "should I buy more NVDA this week?"
- "prepare a buy order for 2 shares of AAPL"

### Tool results
Structured outputs from portfolio, broker, and research tools are fed back into the same model loop.

## Telegram command design
Add these commands in Phase 1.5:
- `/connect_model` starts credential binding
- `/model_status` shows provider, model, and connection status
- `/disconnect_model` revokes the current model binding
- `/new_chat` resets the active model conversation
- `/ask <message>` sends a pure chat turn without creating a trade request

Normal plain-text messages should also route into the foundational model once a model is connected.

## OpenAI-specific adapter contract
`OpenAIResponsesGateway` should own:
- API key resolution
- model selection
- conversation persistence
- tool schema registration
- retries and rate-limit handling
- structured response parsing

Suggested request fields:
- `model`
- `input`
- `instructions`
- `conversation` or `previous_response_id`
- `tools`
- `parallel_tool_calls=false` in v1 unless tool concurrency is intentionally supported

## Security and safety constraints
- keep OpenAI credentials server-side only
- never paste secrets back into Telegram
- store per-user conversation state separately from broker credentials
- block any execution tool until approval state is `approved`
- log every model-produced trade proposal with:
  - user id
  - model id
  - prompt hash
  - tool outputs used
  - approval decision
  - final broker result

## Phase 1
- Telegram surface agent
- Robinhood portfolio read access
- Manual approval before order execution
- Basic holding summary and buy/sell request flow
- Foundational model binding and chat session support
- OpenAI Responses gateway with constrained tool access

## Phase 2
- Research agent for news, earnings, and industry context
- Technical signal layer with RSI and simple trend checks
- Risk gate and approval policy

## Phase 3
- Multi-agent decision loop
- Trade rationale generation
- Better alerting and portfolio monitoring

## Safety rules
- No autonomous execution in v1
- All trades require user approval
- Keep logs for every order request and result
- Prefer explainable decisions over opaque black-box actions
- Do not expose raw provider secrets in Telegram
- Do not allow direct model-to-broker execution without approval middleware
