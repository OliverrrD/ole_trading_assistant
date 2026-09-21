"""SQLite persistence for instruments and per-user watchlists."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.ibkr_client import IbkrContract


@dataclass(frozen=True)
class WatchlistItem:
    user_id: str
    conid: int
    symbol: str
    company_name: str
    listing_exchange: str
    created_at: str


@dataclass(frozen=True)
class AgentObservation:
    observation_id: int
    user_id: str
    user_message: str
    assistant_response: str
    watchlist_symbols: str
    context_hash: str
    model: str
    response_id: str | None
    created_at: str


@dataclass(frozen=True)
class StockObservation:
    observation_id: int
    user_id: str
    user_message: str
    assistant_response: str
    watchlist_symbols: str
    context_hash: str
    model: str
    response_id: str | None
    created_at: str
    conid: int
    symbol: str
    memory_type: str
    relation_type: str
    relevance: float
    is_primary: int


@dataclass(frozen=True)
class MarketBar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None


@dataclass(frozen=True)
class TechnicalSnapshot:
    conid: int
    timeframe: str
    calculated_at: str
    latest_bar_time_ms: int
    close: float
    sma20: float | None
    sma50: float | None
    ema12: float | None
    ema26: float | None
    rsi14: float | None
    atr14: float | None
    return_1bar_pct: float | None
    return_5bar_pct: float | None
    return_20bar_pct: float | None
    volume_ratio20: float | None


class MarketStore:
    def __init__(self, database_path: str):
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = path
        self._initialize()

    def add_to_watchlist(self, user_id: str, contract: IbkrContract) -> bool:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO instruments (conid, symbol, company_name, listing_exchange, currency)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conid) DO UPDATE SET
                    symbol = excluded.symbol,
                    company_name = excluded.company_name,
                    listing_exchange = excluded.listing_exchange,
                    currency = excluded.currency
                """,
                (
                    contract.conid,
                    contract.symbol,
                    contract.company_name,
                    contract.listing_exchange,
                    contract.currency,
                ),
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO watchlist (user_id, conid, created_at)
                VALUES (?, ?, ?)
                """,
                (user_id, contract.conid, created_at),
            )
        return cursor.rowcount > 0

    def remove_from_watchlist(self, user_id: str, symbol: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM watchlist
                WHERE user_id = ? AND conid IN (
                    SELECT conid FROM instruments WHERE symbol = ?
                )
                """,
                (user_id, symbol.upper()),
            )
        return cursor.rowcount > 0

    def list_watchlist(self, user_id: str) -> list[WatchlistItem]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT w.user_id, i.conid, i.symbol, i.company_name,
                       i.listing_exchange, w.created_at
                FROM watchlist AS w
                JOIN instruments AS i ON i.conid = w.conid
                WHERE w.user_id = ?
                ORDER BY i.symbol
                """,
                (user_id,),
            ).fetchall()
        return [WatchlistItem(**dict(row)) for row in rows]

    def find_instrument(self, symbol: str) -> IbkrContract | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT conid, symbol, company_name, currency, listing_exchange
                FROM instruments
                WHERE symbol = ?
                ORDER BY conid
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
        return IbkrContract(**dict(row)) if row else None

    def list_watched_instruments(self) -> list[IbkrContract]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT i.conid, i.symbol, i.company_name,
                       i.currency, i.listing_exchange
                FROM instruments AS i
                JOIN watchlist AS w ON w.conid = i.conid
                ORDER BY i.symbol
                """
            ).fetchall()
        return [IbkrContract(**dict(row)) for row in rows]

    def list_user_instruments(self, user_id: str) -> list[IbkrContract]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.conid, i.symbol, i.company_name,
                       i.currency, i.listing_exchange
                FROM instruments AS i
                JOIN watchlist AS w ON w.conid = i.conid
                WHERE w.user_id = ?
                ORDER BY LENGTH(i.symbol) DESC, i.symbol
                """,
                (user_id,),
            ).fetchall()
        return [IbkrContract(**dict(row)) for row in rows]

    def upsert_market_bars(
        self,
        conid: int,
        timeframe: str,
        bars: list[MarketBar],
        *,
        source: str,
        data_mode: str,
    ) -> int:
        if not bars:
            return 0
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            (
                conid,
                timeframe,
                bar.timestamp_ms,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                source,
                data_mode,
                updated_at,
            )
            for bar in bars
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO market_bars (
                    conid, timeframe, bar_time_ms, open, high, low, close,
                    volume, source, data_mode, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conid, timeframe, bar_time_ms) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    source = excluded.source,
                    data_mode = excluded.data_mode,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_market_bars(self, conid: int, timeframe: str, limit: int = 300) -> list[MarketBar]:
        safe_limit = min(max(1, limit), 5000)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bar_time_ms AS timestamp_ms, open, high, low, close, volume
                FROM market_bars
                WHERE conid = ? AND timeframe = ?
                ORDER BY bar_time_ms DESC
                LIMIT ?
                """,
                (conid, timeframe, safe_limit),
            ).fetchall()
        return [MarketBar(**dict(row)) for row in reversed(rows)]

    def market_bar_counts(self, conid: int) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timeframe, COUNT(*) AS count
                FROM market_bars
                WHERE conid = ?
                GROUP BY timeframe
                ORDER BY timeframe
                """,
                (conid,),
            ).fetchall()
        return {str(row["timeframe"]): int(row["count"]) for row in rows}

    def save_technical_snapshot(self, snapshot: TechnicalSnapshot) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO technical_snapshots (
                    conid, timeframe, calculated_at, latest_bar_time_ms, close,
                    sma20, sma50, ema12, ema26, rsi14, atr14,
                    return_1bar_pct, return_5bar_pct, return_20bar_pct,
                    volume_ratio20
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conid, timeframe) DO UPDATE SET
                    calculated_at = excluded.calculated_at,
                    latest_bar_time_ms = excluded.latest_bar_time_ms,
                    close = excluded.close,
                    sma20 = excluded.sma20,
                    sma50 = excluded.sma50,
                    ema12 = excluded.ema12,
                    ema26 = excluded.ema26,
                    rsi14 = excluded.rsi14,
                    atr14 = excluded.atr14,
                    return_1bar_pct = excluded.return_1bar_pct,
                    return_5bar_pct = excluded.return_5bar_pct,
                    return_20bar_pct = excluded.return_20bar_pct,
                    volume_ratio20 = excluded.volume_ratio20
                """,
                (
                    snapshot.conid,
                    snapshot.timeframe,
                    snapshot.calculated_at,
                    snapshot.latest_bar_time_ms,
                    snapshot.close,
                    snapshot.sma20,
                    snapshot.sma50,
                    snapshot.ema12,
                    snapshot.ema26,
                    snapshot.rsi14,
                    snapshot.atr14,
                    snapshot.return_1bar_pct,
                    snapshot.return_5bar_pct,
                    snapshot.return_20bar_pct,
                    snapshot.volume_ratio20,
                ),
            )

    def get_technical_snapshot(self, conid: int, timeframe: str) -> TechnicalSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT conid, timeframe, calculated_at, latest_bar_time_ms, close,
                       sma20, sma50, ema12, ema26, rsi14, atr14,
                       return_1bar_pct, return_5bar_pct, return_20bar_pct,
                       volume_ratio20
                FROM technical_snapshots
                WHERE conid = ? AND timeframe = ?
                """,
                (conid, timeframe),
            ).fetchone()
        return TechnicalSnapshot(**dict(row)) if row else None

    def save_observation(
        self,
        *,
        user_id: str,
        user_message: str,
        assistant_response: str,
        watchlist_symbols: str,
        runtime_context: str,
        model: str,
        response_id: str | None,
        max_entries: int,
        symbol_conids: tuple[int, ...] = (),
        memory_type: str = "analysis",
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        context_hash = hashlib.sha256(runtime_context.encode("utf-8")).hexdigest()
        retention_limit = max(1, max_entries)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_observations (
                    user_id, user_message, assistant_response, watchlist_symbols,
                    context_hash, model, response_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    user_message,
                    assistant_response,
                    watchlist_symbols,
                    context_hash,
                    model,
                    response_id,
                    created_at,
                ),
            )
            observation_id = int(cursor.lastrowid)
            for index, conid in enumerate(dict.fromkeys(symbol_conids)):
                connection.execute(
                    """
                    INSERT INTO observation_symbols (
                        observation_id, conid, memory_type, relation_type,
                        relevance, is_primary, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        conid,
                        memory_type,
                        "primary" if index == 0 else "comparison",
                        1.0 if index == 0 else 0.8,
                        1 if index == 0 else 0,
                        created_at,
                    ),
                )
            connection.execute(
                """
                DELETE FROM agent_observations
                WHERE user_id = ? AND observation_id NOT IN (
                    SELECT observation_id
                    FROM agent_observations
                    WHERE user_id = ?
                    ORDER BY observation_id DESC
                    LIMIT ?
                )
                """,
                (user_id, user_id, retention_limit),
            )

    def save_tool_executions(
        self,
        *,
        user_id: str,
        response_id: str | None,
        executions: tuple[object, ...],
        max_conversations: int,
    ) -> None:
        if not executions:
            return
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [
            (
                user_id,
                response_id,
                sequence,
                str(getattr(execution, "name")),
                json.dumps(
                    getattr(execution, "arguments"),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                1 if bool(getattr(execution, "success")) else 0,
                created_at,
            )
            for sequence, execution in enumerate(executions, start=1)
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO agent_tool_calls (
                    user_id, response_id, sequence, tool_name,
                    arguments_json, success, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                DELETE FROM agent_tool_calls
                WHERE user_id = ? AND tool_call_id NOT IN (
                    SELECT tool_call_id
                    FROM agent_tool_calls
                    WHERE user_id = ?
                    ORDER BY tool_call_id DESC
                    LIMIT ?
                )
                """,
                (user_id, user_id, max(8, max_conversations * 8)),
            )

    def recent_observations(self, user_id: str, limit: int = 5) -> list[AgentObservation]:
        safe_limit = min(max(1, limit), 20)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT observation_id, user_id, user_message, assistant_response,
                       watchlist_symbols, context_hash, model, response_id, created_at
                FROM agent_observations
                WHERE user_id = ?
                ORDER BY observation_id DESC
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [AgentObservation(**dict(row)) for row in reversed(rows)]

    def observations_without_symbol_index(
        self,
        user_id: str,
        limit: int = 500,
    ) -> list[AgentObservation]:
        safe_limit = min(max(1, limit), 2000)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.observation_id, o.user_id, o.user_message,
                       o.assistant_response, o.watchlist_symbols, o.context_hash,
                       o.model, o.response_id, o.created_at
                FROM agent_observations AS o
                WHERE o.user_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM observation_symbols AS links
                      WHERE links.observation_id = o.observation_id
                  )
                ORDER BY o.observation_id
                LIMIT ?
                """,
                (user_id, safe_limit),
            ).fetchall()
        return [AgentObservation(**dict(row)) for row in rows]

    def link_observation_symbols(
        self,
        observation_id: int,
        symbol_conids: tuple[int, ...],
        memory_type: str,
        created_at: str,
    ) -> None:
        with self._connect() as connection:
            for index, conid in enumerate(dict.fromkeys(symbol_conids)):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO observation_symbols (
                        observation_id, conid, memory_type, relation_type,
                        relevance, is_primary, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        conid,
                        memory_type,
                        "primary" if index == 0 else "comparison",
                        1.0 if index == 0 else 0.8,
                        1 if index == 0 else 0,
                        created_at,
                    ),
                )

    def clear_observations(self, user_id: str) -> int:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM agent_tool_calls WHERE user_id = ?",
                (user_id,),
            )
            cursor = connection.execute(
                "DELETE FROM agent_observations WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount

    def recent_symbol_observations(
        self,
        user_id: str,
        conid: int,
        limit: int = 5,
    ) -> list[StockObservation]:
        safe_limit = min(max(1, limit), 20)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.observation_id, o.user_id, o.user_message,
                       o.assistant_response, o.watchlist_symbols, o.context_hash,
                       o.model, o.response_id, o.created_at, links.conid,
                       i.symbol, links.memory_type, links.relation_type,
                       links.relevance, links.is_primary
                FROM observation_symbols AS links
                JOIN agent_observations AS o
                  ON o.observation_id = links.observation_id
                JOIN instruments AS i ON i.conid = links.conid
                WHERE o.user_id = ? AND links.conid = ?
                ORDER BY o.observation_id DESC
                LIMIT ?
                """,
                (user_id, conid, safe_limit),
            ).fetchall()
        return [StockObservation(**dict(row)) for row in reversed(rows)]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    conid INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    company_name TEXT NOT NULL,
                    listing_exchange TEXT NOT NULL,
                    currency TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_instruments_symbol
                ON instruments(symbol);

                CREATE TABLE IF NOT EXISTS watchlist (
                    user_id TEXT NOT NULL,
                    conid INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, conid),
                    FOREIGN KEY (conid) REFERENCES instruments(conid)
                );

                CREATE TABLE IF NOT EXISTS agent_observations (
                    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_response TEXT NOT NULL,
                    watchlist_symbols TEXT NOT NULL,
                    context_hash TEXT NOT NULL,
                    model TEXT NOT NULL,
                    response_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_observations_user_time
                ON agent_observations(user_id, observation_id DESC);

                CREATE TABLE IF NOT EXISTS agent_tool_calls (
                    tool_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    response_id TEXT,
                    sequence INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_user_time
                ON agent_tool_calls(user_id, tool_call_id DESC);

                CREATE TABLE IF NOT EXISTS observation_symbols (
                    observation_id INTEGER NOT NULL,
                    conid INTEGER NOT NULL,
                    memory_type TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    relevance REAL NOT NULL,
                    is_primary INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (observation_id, conid),
                    FOREIGN KEY (observation_id)
                        REFERENCES agent_observations(observation_id) ON DELETE CASCADE,
                    FOREIGN KEY (conid) REFERENCES instruments(conid)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_observation_symbols_lookup
                ON observation_symbols(conid, observation_id DESC);

                CREATE TABLE IF NOT EXISTS market_bars (
                    conid INTEGER NOT NULL,
                    timeframe TEXT NOT NULL,
                    bar_time_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL,
                    source TEXT NOT NULL,
                    data_mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (conid, timeframe, bar_time_ms),
                    FOREIGN KEY (conid) REFERENCES instruments(conid)
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_market_bars_time
                ON market_bars(timeframe, bar_time_ms);

                CREATE TABLE IF NOT EXISTS technical_snapshots (
                    conid INTEGER NOT NULL,
                    timeframe TEXT NOT NULL,
                    calculated_at TEXT NOT NULL,
                    latest_bar_time_ms INTEGER NOT NULL,
                    close REAL NOT NULL,
                    sma20 REAL,
                    sma50 REAL,
                    ema12 REAL,
                    ema26 REAL,
                    rsi14 REAL,
                    atr14 REAL,
                    return_1bar_pct REAL,
                    return_5bar_pct REAL,
                    return_20bar_pct REAL,
                    volume_ratio20 REAL,
                    PRIMARY KEY (conid, timeframe),
                    FOREIGN KEY (conid) REFERENCES instruments(conid)
                ) WITHOUT ROWID;
                """
            )
