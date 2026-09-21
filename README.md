# Ole Trading Assistant

This is a trading assistant agent system.

## Setup

1. Install dependencies:
   `pip install -r requirements.txt`
2. Copy `.env.example` to `.env`
3. Fill in:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_ALLOWED_USER_IDS` (comma-separated Telegram numeric user IDs)
   - `OPENAI_API_KEY`
   - `OPENAI_MODEL` (default: `gpt-5-codex`)
   - `IBKR_BASE_URL` (default: `https://localhost:5000/v1/api`)
   - optionally `IBKR_ACCOUNT_ID` if you want to pin a specific account
4. Start the bot:
   `python main.py`

If the allowlist is empty, the bot rejects messages and replies with the sender's numeric Telegram user ID. Add your own ID to `.env`, for example `TELEGRAM_ALLOWED_USER_IDS=123456789`, then restart the bot.

## IBKR Setup

1. Install and run IBKR Client Portal Gateway on the same machine as this bot.
2. Open `https://localhost:5000` in your browser and log in to IBKR.
3. Keep the gateway running while the bot is running.
4. If your local certificate is unsigned, keep `IBKR_VERIFY_SSL=false`.
5. Use `/ibkr_status`, `/accounts`, and `/portfolio` in Telegram to validate the connection.

## Market Watchlist

The watchlist is stored per Telegram user in local SQLite at `data/market.db` by default. Override it with `MARKET_DATABASE_PATH`. Adding a symbol performs a layered IBKR historical backfill and stores market bars:

- `1m`: previous day with one-minute bars
- `10m`: previous week with ten-minute bars
- `1h`: previous month with hourly bars
- `1d`: previous year with daily bars

The background monitor refreshes one-minute history for all watched instruments every five minutes by default. This is near-real-time closed-bar synchronization for low-frequency trading, not raw-tick WebSocket capture. Configure it with `MARKET_SYNC_ENABLED` and `MARKET_SYNC_INTERVAL_SECONDS`.

After each history update, the service calculates deterministic SMA20, SMA50, EMA12, EMA26, RSI14, ATR14, 1/5/20-bar returns, and current-volume-to-prior-20 average. IBKR supplies OHLCV bars; these indicators are calculated locally.

Example flow:

1. `/watch AAPL`
2. `/watchlist`
3. `/quote AAPL`
4. `/technical AAPL 1d`
5. `/market_monitor`
6. `/unwatch AAPL`

IBKR may return real-time, delayed, frozen, or unavailable prices depending on the active session and market-data subscriptions. The quote response displays that data mode explicitly.

## Local Agent State

Successful model conversations are saved in the local SQLite `agent_observations` table. Each record contains the user message, assistant response, Watchlist summary, model response ID, and a hash of the runtime context. Full portfolio snapshots and credentials are not stored in this table.

When a message explicitly mentions one or more symbols already present in that user's Watchlist, the conversation is indexed to those IBKR conids in `observation_symbols`. The system classifies the record as `analysis`, `decision`, `risk`, `thesis`, `trade_idea`, or `outcome` using deterministic keywords. A later question mentioning the same symbol retrieves up to five related records and marks them as historical opinions rather than current market facts.

On the first model turn after a bot restart, the three most recent saved conversations are supplied as historical context. This is a small recency-based retrieval layer, not semantic vector search. Use `AGENT_HISTORY_MAX_ENTRIES` to control per-user retention; the default is 200.

The SQLite file is not encrypted. Keep `data/market.db` private and use `/clear_history CONFIRM` to delete your saved conversation history and reset the active model session.

## Natural-language orchestration

Plain text and `/ask` messages use a bounded OpenAI tool-calling loop. The model can decide to read the current Watchlist, an IBKR quote, deterministic technical indicators, the IBKR portfolio, paper-order status, or stock-indexed conversation memory. Commands remain useful as deterministic shortcuts, but they are not required for these read operations.

The loop is limited to four rounds and eight tool calls per message, and tool calls run sequentially. It exposes no tool for placing, modifying, confirming, or cancelling an order. A natural-language request such as “buy one share of AAPL” must be redirected to the explicit `/order` and `/confirm` workflow.

Tool names, arguments, and success status are saved to the local `agent_tool_calls` audit table. Tool outputs are not saved there, and responses using portfolio or active-order tools are redacted from local conversation history. When `IBKR_INCLUDE_ACCOUNT_CONTEXT=true`, a portfolio question can send the returned compact account snapshot to the configured OpenAI endpoint so the model can answer it; keep this setting false if that is not acceptable.

## Paper Trading

Trading is disabled by default. To enable the order workflow:

1. Create or activate an IBKR Paper Trading Account.
2. Restart Client Portal Gateway and log in with the paper username and password.
3. Set the explicit `DU...` paper account ID and enable paper mode:
   ```env
   IBKR_ACCOUNT_ID=DU1234567
   IBKR_TRADING_MODE=paper
   IBKR_MAX_ORDER_NOTIONAL_USD=1000
   IBKR_MAX_DAILY_NOTIONAL_USD=5000
   IBKR_ORDER_CONFIRMATION_TTL_SECONDS=300
   ```
4. Restart the bot and verify `/accounts` before drafting an order.

The service requires both a `DU...` account ID and IBKR's `isPaper=true` session flag. It supports only whole-share US stock/ETF limit orders, `DAY` time in force, and regular trading hours. Chat messages cannot place orders.

Example flow:

1. `/order buy AAPL 1 lmt 150`
2. Review the IBKR What-If preview.
3. `/confirm <token>`
4. If IBKR returns a warning, use `/confirm_reply <token>` or `/cancel <token>` before any other broker command.
5. Use `/orders` or `/order_status <order_id>` to monitor the order.

## Telegram commands

- `/start` show quick help
- `/setup` show config checklist
- `/model_status` show foundational model status
- `/status` show assistant runtime status
- `/ibkr_status` show IBKR gateway authentication status
- `/accounts` list IBKR accounts visible to the gateway
- `/portfolio` show an IBKR portfolio snapshot
- `/watch <symbol>` resolve an IBKR contract and add it to your persistent watchlist
- `/unwatch <symbol>` remove a symbol from your watchlist
- `/watchlist` list your saved symbols
- `/quote <symbol>` fetch an on-demand IBKR quote and show its data mode
- `/technical <symbol> [1m|10m|1h|1d]` refresh and display stored technical indicators
- `/market_sync` immediately synchronize recent one-minute bars for the Watchlist
- `/market_monitor` show background synchronization status
- `/history [symbol]` show the five most recent conversations overall or for one watched symbol
- `/clear_history CONFIRM` delete saved conversations and reset the active chat
- `/order <buy|sell> <symbol> <quantity> [lmt] <price>` preview a paper limit order
- `/confirm <token>` submit a reviewed paper order
- `/confirm_reply <token>` accept an IBKR order warning
- `/cancel <token>` cancel a draft or decline an IBKR order warning
- `/orders` list orders from the current paper session
- `/order_status <order_id>` query a single paper order
- `/cancel_order <order_id>` request cancellation of an active paper order
- `/reset_chat` clear the current model conversation
- `/ask <message>` send a message to the model

Once `OPENAI_API_KEY` is configured, plain text messages are also sent to the foundational model.
If `IBKR_INCLUDE_ACCOUNT_CONTEXT=true`, the orchestrator may fetch and send a compact IBKR portfolio snapshot to the model when the conversation needs it.
