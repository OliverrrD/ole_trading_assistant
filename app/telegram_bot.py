"""Minimal Telegram surface agent entrypoint."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, CommandHandler, ContextTypes, MessageHandler, filters

from app.assistant_service import TradingAssistantService


class TelegramBot:
    def __init__(self, token: str, assistant_service: TradingAssistantService):
        self.token = token
        self.assistant_service = assistant_service
        self.application = Application.builder().token(token).build()

    async def authorize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user and str(user.id) in self.assistant_service.settings.telegram_allowed_user_ids:
            return
        if update.message and user:
            await update.message.reply_text(
                "Access denied. "
                f"Your Telegram user ID is {user.id}. "
                "Add it to TELEGRAM_ALLOWED_USER_IDS in .env and restart the bot."
            )
        raise ApplicationHandlerStop

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return

        await update.message.reply_text(
            "Ole Trading Assistant is ready.\n"
            "Use /setup to check configuration, /model_status to inspect the model gateway, "
            "/status to inspect assistant status, /ibkr_status to inspect the IBKR gateway, "
            "/accounts and /portfolio to read IBKR data, /order to draft a paper order, "
            "/watch, /quote, and /technical to inspect market data, /orders to query paper orders, "
            "/reset_chat to start fresh, or ask in plain language and let the read-only "
            "orchestrator choose the appropriate data tool."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        message = self.assistant_service.assistant_status(str(update.effective_user.id))
        await update.message.reply_text(message)

    async def setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(self.assistant_service.format_setup_message())

    async def model_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        user_id = str(update.effective_user.id)
        await update.message.reply_text(self.assistant_service.format_model_status(user_id))

    async def reset_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        user_id = str(update.effective_user.id)
        await update.message.reply_text(self.assistant_service.reset_chat(user_id))

    async def ibkr_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        message = await asyncio.to_thread(self.assistant_service.ibkr_status)
        await update.message.reply_text(message)

    async def accounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        message = await asyncio.to_thread(self.assistant_service.ibkr_accounts)
        await update.message.reply_text(message)

    async def portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        message = await asyncio.to_thread(self.assistant_service.ibkr_portfolio)
        await update.message.reply_text(message)

    async def watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        symbol = context.args[0].strip() if len(context.args) == 1 else ""
        if not symbol:
            await update.message.reply_text("Usage: /watch <symbol>")
            return
        message = await asyncio.to_thread(
            self.assistant_service.add_watch,
            str(update.effective_user.id),
            symbol,
        )
        await update.message.reply_text(message)

    async def unwatch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        symbol = context.args[0].strip() if len(context.args) == 1 else ""
        if not symbol:
            await update.message.reply_text("Usage: /unwatch <symbol>")
            return
        message = await asyncio.to_thread(
            self.assistant_service.remove_watch,
            str(update.effective_user.id),
            symbol,
        )
        await update.message.reply_text(message)

    async def watchlist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        message = await asyncio.to_thread(
            self.assistant_service.watchlist,
            str(update.effective_user.id),
        )
        await update.message.reply_text(message)

    async def quote(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        symbol = context.args[0].strip() if len(context.args) == 1 else ""
        if not symbol:
            await update.message.reply_text("Usage: /quote <symbol>")
            return
        message = await asyncio.to_thread(self.assistant_service.quote, symbol)
        await update.message.reply_text(message)

    async def technical(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        if not 1 <= len(context.args) <= 2:
            await update.message.reply_text("Usage: /technical <symbol> [1m|10m|1h|1d]")
            return
        symbol = context.args[0].strip()
        timeframe = context.args[1].strip() if len(context.args) == 2 else "1d"
        message = await asyncio.to_thread(self.assistant_service.technical, symbol, timeframe)
        await update.message.reply_text(message)

    async def market_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        message = await asyncio.to_thread(self.assistant_service.market_sync)
        await update.message.reply_text(message)

    async def market_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(self.assistant_service.market_monitor_status())

    async def history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        if len(context.args) > 1:
            await update.message.reply_text("Usage: /history [symbol]")
            return
        symbol = context.args[0].strip() if context.args else ""
        message = await asyncio.to_thread(
            self.assistant_service.conversation_history,
            str(update.effective_user.id),
            symbol,
        )
        await update.message.reply_text(message)

    async def clear_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        confirmation = context.args[0] if len(context.args) == 1 else ""
        message = await asyncio.to_thread(
            self.assistant_service.clear_conversation_history,
            str(update.effective_user.id),
            confirmation,
        )
        await update.message.reply_text(message)

    async def order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        message = await asyncio.to_thread(
            self.assistant_service.create_order,
            str(update.effective_user.id),
            list(context.args),
        )
        await update.message.reply_text(message)

    async def confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        token = context.args[0].strip() if len(context.args) == 1 else ""
        if not token:
            await update.message.reply_text("Usage: /confirm <order token>")
            return
        message = await asyncio.to_thread(
            self.assistant_service.confirm_order,
            str(update.effective_user.id),
            token,
        )
        await update.message.reply_text(message)

    async def confirm_reply(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        token = context.args[0].strip() if len(context.args) == 1 else ""
        if not token:
            await update.message.reply_text("Usage: /confirm_reply <order token>")
            return
        message = await asyncio.to_thread(
            self.assistant_service.confirm_order_reply,
            str(update.effective_user.id),
            token,
        )
        await update.message.reply_text(message)

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        token = context.args[0].strip() if len(context.args) == 1 else ""
        if not token:
            await update.message.reply_text("Usage: /cancel <order token>")
            return
        message = await asyncio.to_thread(
            self.assistant_service.cancel_pending_order,
            str(update.effective_user.id),
            token,
        )
        await update.message.reply_text(message)

    async def orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        message = await asyncio.to_thread(self.assistant_service.list_orders)
        await update.message.reply_text(message)

    async def order_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        order_id = context.args[0].strip() if len(context.args) == 1 else ""
        message = await asyncio.to_thread(self.assistant_service.order_status, order_id)
        await update.message.reply_text(message)

    async def cancel_order(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        order_id = context.args[0].strip() if len(context.args) == 1 else ""
        message = await asyncio.to_thread(self.assistant_service.cancel_live_order, order_id)
        await update.message.reply_text(message)

    async def ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        prompt = " ".join(context.args).strip()
        if not prompt:
            await update.message.reply_text("Usage: /ask <your question>")
            return
        reply = await asyncio.to_thread(self.assistant_service.chat, str(update.effective_user.id), prompt)
        await update.message.reply_text(reply)

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user or not update.message.text:
            return
        reply = await asyncio.to_thread(
            self.assistant_service.chat,
            str(update.effective_user.id),
            update.message.text,
        )
        await update.message.reply_text(reply)

    def register_handlers(self) -> None:
        self.application.add_handler(MessageHandler(filters.ALL, self.authorize), group=-1)
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("setup", self.setup))
        self.application.add_handler(CommandHandler("model_status", self.model_status))
        self.application.add_handler(CommandHandler("status", self.status))
        self.application.add_handler(CommandHandler("ibkr_status", self.ibkr_status))
        self.application.add_handler(CommandHandler("accounts", self.accounts))
        self.application.add_handler(CommandHandler("portfolio", self.portfolio))
        self.application.add_handler(CommandHandler("watch", self.watch))
        self.application.add_handler(CommandHandler("unwatch", self.unwatch))
        self.application.add_handler(CommandHandler("watchlist", self.watchlist))
        self.application.add_handler(CommandHandler("quote", self.quote))
        self.application.add_handler(CommandHandler("technical", self.technical))
        self.application.add_handler(CommandHandler("market_sync", self.market_sync))
        self.application.add_handler(CommandHandler("market_monitor", self.market_monitor))
        self.application.add_handler(CommandHandler("history", self.history))
        self.application.add_handler(CommandHandler("clear_history", self.clear_history))
        self.application.add_handler(CommandHandler("order", self.order))
        self.application.add_handler(CommandHandler("confirm", self.confirm))
        self.application.add_handler(CommandHandler("confirm_reply", self.confirm_reply))
        self.application.add_handler(CommandHandler("cancel", self.cancel))
        self.application.add_handler(CommandHandler("orders", self.orders))
        self.application.add_handler(CommandHandler("order_status", self.order_status))
        self.application.add_handler(CommandHandler("cancel_order", self.cancel_order))
        self.application.add_handler(CommandHandler("reset_chat", self.reset_chat))
        self.application.add_handler(CommandHandler("ask", self.ask))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))

    def run(self) -> None:
        self.register_handlers()
        self.assistant_service.start_market_monitor()
        try:
            self.application.run_polling()
        finally:
            self.assistant_service.stop_market_monitor()
