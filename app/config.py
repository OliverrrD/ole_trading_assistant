import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_csv(name: str) -> frozenset[str]:
    value = os.getenv(name, "")
    return frozenset(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_allowed_user_ids: frozenset[str] = _env_csv("TELEGRAM_ALLOWED_USER_IDS")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5-codex")
    openai_org_id: str = os.getenv("OPENAI_ORG_ID", "")
    openai_project_id: str = os.getenv("OPENAI_PROJECT_ID", "")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_reasoning_effort: str = os.getenv("OPENAI_REASONING_EFFORT", "")
    openai_text_verbosity: str = os.getenv("OPENAI_TEXT_VERBOSITY", "low")
    assistant_name: str = os.getenv("ASSISTANT_NAME", "Ole")
    ibkr_base_url: str = os.getenv("IBKR_BASE_URL", "https://localhost:5000/v1/api")
    ibkr_account_id: str = os.getenv("IBKR_ACCOUNT_ID", "")
    ibkr_verify_ssl: bool = _env_flag("IBKR_VERIFY_SSL", False)
    ibkr_include_account_context: bool = _env_flag("IBKR_INCLUDE_ACCOUNT_CONTEXT", True)
    ibkr_trading_mode: str = os.getenv("IBKR_TRADING_MODE", "disabled").strip().lower()
    ibkr_max_order_notional_usd: float = _env_float("IBKR_MAX_ORDER_NOTIONAL_USD", 1000.0)
    ibkr_max_daily_notional_usd: float = _env_float("IBKR_MAX_DAILY_NOTIONAL_USD", 5000.0)
    ibkr_order_confirmation_ttl_seconds: int = _env_int("IBKR_ORDER_CONFIRMATION_TTL_SECONDS", 300)
    market_database_path: str = os.getenv("MARKET_DATABASE_PATH", "data/market.db")
    market_sync_enabled: bool = _env_flag("MARKET_SYNC_ENABLED", True)
    market_sync_interval_seconds: int = _env_int("MARKET_SYNC_INTERVAL_SECONDS", 300)
    agent_history_max_entries: int = _env_int("AGENT_HISTORY_MAX_ENTRIES", 200)
    approval_mode: str = os.getenv("APPROVAL_MODE", "manual")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    @property
    def has_telegram_config(self) -> bool:
        return bool(self.telegram_bot_token)

    @property
    def has_openai_config(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def has_ibkr_config(self) -> bool:
        return bool(self.ibkr_base_url)


settings = Settings()
