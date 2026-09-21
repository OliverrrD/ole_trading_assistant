from app.assistant_service import TradingAssistantService
from app.config import settings
from app.telegram_bot import TelegramBot


def main() -> None:
    if not settings.has_telegram_config:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing. Add it to your .env file.")

    assistant_service = TradingAssistantService(settings)
    bot = TelegramBot(settings.telegram_bot_token, assistant_service)
    print("Starting Telegram bot...")
    bot.run()


if __name__ == "__main__":
    main()
