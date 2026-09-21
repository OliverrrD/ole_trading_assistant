"""Watchlist management, historical bars, and technical snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.ibkr_client import IbkrClient, IbkrContract, IbkrQuote
from app.market_store import MarketBar, MarketStore, TechnicalSnapshot
from app.technical_analysis import TechnicalAnalyzer


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
HISTORY_SPECS = {
    "1m": ("1d", "1min"),
    "10m": ("1w", "10min"),
    "1h": ("1m", "1h"),
    "1d": ("1y", "1d"),
}


@dataclass(frozen=True)
class MarketSyncResult:
    symbols: int
    bars: int
    errors: tuple[str, ...]


class MarketDataService:
    def __init__(self, ibkr_client: IbkrClient, store: MarketStore):
        self.ibkr_client = ibkr_client
        self.store = store
        self.technical_analyzer = TechnicalAnalyzer()

    def add_watch(self, user_id: str, symbol: str) -> str:
        try:
            normalized_symbol = self._validate_symbol(symbol)
            contract = self.ibkr_client.resolve_us_stock(normalized_symbol)
            added = self.store.add_to_watchlist(user_id, contract)
            counts, errors = self.backfill_contract(contract)
            action = "Added" if added else "Refreshed"
            lines = [
                f"{action} {contract.symbol} on your watchlist.",
                f"- company: {contract.company_name}",
                f"- exchange: {contract.listing_exchange}",
                f"- conid: {contract.conid}",
                "- stored bars: " + ", ".join(
                    f"{timeframe}={count}" for timeframe, count in counts.items()
                ),
            ]
            if errors:
                lines.append("- history warnings: " + " | ".join(errors))
            return "\n".join(lines)
        except Exception as exc:
            return f"Could not add watch: {exc}"

    def remove_watch(self, user_id: str, symbol: str) -> str:
        try:
            normalized_symbol = self._validate_symbol(symbol)
            if not self.store.remove_from_watchlist(user_id, normalized_symbol):
                return f"{normalized_symbol} is not on your watchlist."
            return f"Removed {normalized_symbol} from your watchlist."
        except Exception as exc:
            return f"Could not remove watch: {exc}"

    def format_watchlist(self, user_id: str) -> str:
        items = self.store.list_watchlist(user_id)
        if not items:
            return "Your watchlist is empty. Use /watch <symbol> to add one."
        lines = ["Your watchlist:"]
        for item in items:
            lines.append(
                f"- {item.symbol}: {item.company_name} "
                f"({item.listing_exchange}, conid={item.conid})"
            )
        return "\n".join(lines)

    def format_quote(self, symbol: str) -> str:
        try:
            normalized_symbol = self._validate_symbol(symbol)
            contract = self.store.find_instrument(normalized_symbol)
            if contract is None:
                contract = self.ibkr_client.resolve_us_stock(normalized_symbol)
            quote = self.ibkr_client.get_quote(contract.conid)
            return self._format_quote_result(contract, quote)
        except Exception as exc:
            return f"Quote lookup failed: {exc}"

    def backfill_contract(self, contract: IbkrContract) -> tuple[dict[str, int], list[str]]:
        counts: dict[str, int] = {}
        errors: list[str] = []
        for timeframe in HISTORY_SPECS:
            try:
                counts[timeframe] = self.sync_contract_timeframe(contract, timeframe)
            except Exception as exc:
                errors.append(f"{timeframe}: {exc}")
        return counts, errors

    def sync_contract_timeframe(self, contract: IbkrContract, timeframe: str) -> int:
        if timeframe not in HISTORY_SPECS:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        period, ibkr_bar = HISTORY_SPECS[timeframe]
        historical_bars, data_mode = self.ibkr_client.get_historical_bars(
            contract.conid,
            period=period,
            bar=ibkr_bar,
            outside_rth=False,
            source="Last",
        )
        bars = [
            MarketBar(
                timestamp_ms=item.timestamp_ms,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
            )
            for item in historical_bars
        ]
        self.store.upsert_market_bars(
            contract.conid,
            timeframe,
            bars,
            source="IBKR:Last",
            data_mode=data_mode,
        )
        self._calculate_and_store(contract.conid, timeframe)
        return len(bars)

    def sync_watchlist_once(self) -> MarketSyncResult:
        contracts = self.store.list_watched_instruments()
        stored_bars = 0
        errors: list[str] = []
        for contract in contracts:
            try:
                stored_bars += self.sync_contract_timeframe(contract, "1m")
            except Exception as exc:
                errors.append(f"{contract.symbol}: {exc}")
        return MarketSyncResult(len(contracts), stored_bars, tuple(errors))

    def format_technical(self, symbol: str, timeframe: str = "1d") -> str:
        try:
            normalized_symbol = self._validate_symbol(symbol)
            normalized_timeframe = timeframe.strip().lower() or "1d"
            if normalized_timeframe not in HISTORY_SPECS:
                return "Timeframe must be one of: 1m, 10m, 1h, 1d."
            contract = self.store.find_instrument(normalized_symbol)
            if contract is None:
                return f"{normalized_symbol} has no stored history. Use /watch {normalized_symbol} first."

            refresh_warning = ""
            try:
                self.sync_contract_timeframe(contract, normalized_timeframe)
            except Exception as exc:
                refresh_warning = f"\n- refresh warning: {exc}"

            snapshot = self.store.get_technical_snapshot(contract.conid, normalized_timeframe)
            if snapshot is None:
                return (
                    f"No stored {normalized_timeframe} history for {normalized_symbol}."
                    f"{refresh_warning}"
                )
            counts = self.store.market_bar_counts(contract.conid)
            return self._format_technical_result(contract, snapshot, counts) + refresh_warning
        except Exception as exc:
            return f"Technical analysis failed: {exc}"

    def format_market_sync(self) -> str:
        result = self.sync_watchlist_once()
        lines = [
            "Market sync completed:",
            f"- symbols: {result.symbols}",
            f"- received/upserted 1m bars: {result.bars}",
        ]
        if result.errors:
            lines.append("- errors: " + " | ".join(result.errors[:5]))
        return "\n".join(lines)

    def context_summary(self, user_id: str) -> str:
        symbols = [item.symbol for item in self.store.list_watchlist(user_id)]
        return ", ".join(symbols) if symbols else "none"

    def technical_context_summary(self, user_id: str) -> str:
        lines: list[str] = []
        for item in self.store.list_watchlist(user_id)[:10]:
            snapshot = self.store.get_technical_snapshot(item.conid, "1d")
            if snapshot is None:
                continue
            lines.append(
                f"{item.symbol}: close={snapshot.close:.4f}, "
                f"trend={self.technical_analyzer.trend_label(snapshot)}, "
                f"RSI14={self._number(snapshot.rsi14, 2)}, "
                f"return_20bars={self._percent(snapshot.return_20bar_pct)}, "
                f"calculated_at={snapshot.calculated_at}"
            )
        return "\n".join(lines) if lines else "none stored"

    def extract_user_instruments(self, user_id: str, text: str) -> list[IbkrContract]:
        matches: list[tuple[int, IbkrContract]] = []
        for contract in self.store.list_user_instruments(user_id):
            escaped_symbol = re.escape(contract.symbol)
            explicit_pattern = re.compile(
                rf"(?<![A-Za-z0-9.])\${escaped_symbol}(?![A-Za-z0-9.])",
                re.IGNORECASE,
            )
            match = explicit_pattern.search(text)
            if match is None:
                flags = re.IGNORECASE if len(contract.symbol) >= 4 else 0
                symbol_pattern = re.compile(
                    rf"(?<![A-Za-z0-9.]){escaped_symbol}(?![A-Za-z0-9.])",
                    flags,
                )
                match = symbol_pattern.search(text)
            if match:
                matches.append((match.start(), contract))
        matches.sort(key=lambda item: item[0])
        return [contract for _, contract in matches]

    def _calculate_and_store(self, conid: int, timeframe: str) -> None:
        bars = self.store.get_market_bars(conid, timeframe, limit=1000)
        snapshot = self.technical_analyzer.calculate(conid, timeframe, bars)
        if snapshot:
            self.store.save_technical_snapshot(snapshot)

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        normalized_symbol = symbol.strip().upper()
        if not SYMBOL_PATTERN.fullmatch(normalized_symbol):
            raise ValueError("symbol must be 1-10 letters, digits, dots, or hyphens")
        return normalized_symbol

    @staticmethod
    def _format_quote_result(contract: IbkrContract, quote: IbkrQuote) -> str:
        lines = [f"IBKR quote for {contract.symbol} ({contract.company_name}):"]
        lines.extend(
            [
                f"- last: {quote.last or 'unavailable'}",
                f"- bid / ask: {quote.bid or 'unavailable'} / {quote.ask or 'unavailable'}",
                f"- bid / ask size: {quote.bid_size or 'unavailable'} / {quote.ask_size or 'unavailable'}",
                f"- last size: {quote.last_size or 'unavailable'}",
                f"- data mode: {quote.data_mode}",
                f"- IBKR updated (UTC): {quote.updated_at or 'unavailable'}",
                f"- conid: {contract.conid}",
            ]
        )
        if not any((quote.last, quote.bid, quote.ask)):
            lines.append("- note: no price was returned; check the session and market-data subscription.")
        return "\n".join(lines)

    def _format_technical_result(
        self,
        contract: IbkrContract,
        snapshot: TechnicalSnapshot,
        counts: dict[str, int],
    ) -> str:
        latest_bar_time = datetime.fromtimestamp(
            snapshot.latest_bar_time_ms / 1000,
            timezone.utc,
        ).isoformat(timespec="minutes")
        trend = self.technical_analyzer.trend_label(snapshot)
        return "\n".join(
            [
                f"Technical snapshot for {contract.symbol} ({snapshot.timeframe}):",
                f"- latest close: {snapshot.close:.4f}",
                f"- latest bar (UTC): {latest_bar_time}",
                f"- trend: {trend}",
                f"- SMA20 / SMA50: {self._number(snapshot.sma20)} / {self._number(snapshot.sma50)}",
                f"- EMA12 / EMA26: {self._number(snapshot.ema12)} / {self._number(snapshot.ema26)}",
                f"- RSI14: {self._number(snapshot.rsi14, 2)}",
                f"- ATR14: {self._number(snapshot.atr14)}",
                f"- return 1 / 5 / 20 bars: "
                f"{self._percent(snapshot.return_1bar_pct)} / "
                f"{self._percent(snapshot.return_5bar_pct)} / "
                f"{self._percent(snapshot.return_20bar_pct)}",
                f"- current volume / prior-20 average: {self._number(snapshot.volume_ratio20, 2)}x",
                "- stored bars: " + ", ".join(
                    f"{timeframe}={count}" for timeframe, count in counts.items()
                ),
                "- note: deterministic indicators only; not a trade recommendation.",
            ]
        )

    @staticmethod
    def _number(value: float | None, decimals: int = 4) -> str:
        return "N/A" if value is None else f"{value:.{decimals}f}"

    @staticmethod
    def _percent(value: float | None) -> str:
        return "N/A" if value is None else f"{value:+.2f}%"
