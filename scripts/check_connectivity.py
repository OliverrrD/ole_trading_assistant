from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.assistant_service import TradingAssistantService
from app.config import settings


def _error_label(exc: Exception) -> str:
    status_code = getattr(exc, "status_code", None)
    if status_code:
        return f"{type(exc).__name__} (HTTP {status_code})"
    return type(exc).__name__


def check_openai(service: TradingAssistantService) -> bool:
    if not service.gateway:
        print("OpenAI: failed (OPENAI_API_KEY is missing)")
        return False

    try:
        session = service.gateway.create_session("connectivity-check")
        response = service.gateway.send_user_message(session, "Reply exactly: OK")
    except Exception as exc:
        print(f"OpenAI: failed ({_error_label(exc)})")
        return False

    print(f"OpenAI: connected (model={session.model}, reply={response.text.strip()!r})")
    return True


def check_telegram() -> bool:
    if not settings.telegram_bot_token:
        print("Telegram: failed (TELEGRAM_BOT_TOKEN is missing)")
        return False

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe",
            timeout=20,
        )
        payload = response.json()
    except Exception as exc:
        print(f"Telegram: failed ({_error_label(exc)})")
        return False

    if not response.ok or not payload.get("ok"):
        print(f"Telegram: failed (HTTP {response.status_code})")
        return False

    username = (payload.get("result") or {}).get("username", "unknown")
    print(f"Telegram: connected (bot=@{username})")
    return True


def check_ibkr(service: TradingAssistantService) -> bool:
    try:
        status = service.ibkr_client.auth_status()
    except Exception as exc:
        print(f"IBKR: failed ({_error_label(exc)})")
        try:
            sso_status = service.ibkr_client.validate_sso()
            print(f"IBKR SSO: valid={bool(sso_status.get('RESULT'))}")
        except Exception as sso_exc:
            print(f"IBKR SSO: failed ({_error_label(sso_exc)})")
        return False

    connected = bool(status.get("connected"))
    authenticated = bool(status.get("authenticated"))
    print(f"IBKR: reachable (connected={connected}, authenticated={authenticated})")
    return connected and authenticated


def main() -> int:
    if not settings.ibkr_verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    service = TradingAssistantService(settings)
    checks: tuple[Callable[[], bool], ...] = (
        lambda: check_openai(service),
        check_telegram,
        lambda: check_ibkr(service),
    )
    results = [check() for check in checks]
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
