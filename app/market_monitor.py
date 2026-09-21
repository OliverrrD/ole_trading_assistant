"""Background near-real-time synchronization of watchlist minute bars."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Lock, Thread

from app.market_data_service import MarketDataService


class MarketDataMonitor:
    def __init__(self, service: MarketDataService, interval_seconds: int):
        self.service = service
        self.interval_seconds = max(60, interval_seconds)
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._state_lock = Lock()
        self._last_sync_at: str | None = None
        self._last_result = "not started"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="market-data-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def format_status(self) -> str:
        running = bool(self._thread and self._thread.is_alive())
        with self._state_lock:
            return "\n".join(
                [
                    "Market monitor status:",
                    f"- running: {str(running).lower()}",
                    f"- interval seconds: {self.interval_seconds}",
                    f"- last sync (UTC): {self._last_sync_at or 'never'}",
                    f"- last result: {self._last_result}",
                ]
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self.service.sync_watchlist_once()
                summary = f"symbols={result.symbols}, bars={result.bars}, errors={len(result.errors)}"
            except Exception as exc:
                summary = f"failed: {exc}"
            with self._state_lock:
                self._last_sync_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._last_result = summary
            self._stop_event.wait(self.interval_seconds)
