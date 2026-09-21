"""Deterministic technical indicator calculations for stored market bars."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import fmean

from app.market_store import MarketBar, TechnicalSnapshot


class TechnicalAnalyzer:
    def calculate(
        self,
        conid: int,
        timeframe: str,
        bars: list[MarketBar],
    ) -> TechnicalSnapshot | None:
        if not bars:
            return None
        closes = [bar.close for bar in bars]
        return TechnicalSnapshot(
            conid=conid,
            timeframe=timeframe,
            calculated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            latest_bar_time_ms=bars[-1].timestamp_ms,
            close=closes[-1],
            sma20=self._sma(closes, 20),
            sma50=self._sma(closes, 50),
            ema12=self._ema(closes, 12),
            ema26=self._ema(closes, 26),
            rsi14=self._rsi(closes, 14),
            atr14=self._atr(bars, 14),
            return_1bar_pct=self._return_pct(closes, 1),
            return_5bar_pct=self._return_pct(closes, 5),
            return_20bar_pct=self._return_pct(closes, 20),
            volume_ratio20=self._volume_ratio(bars, 20),
        )

    @staticmethod
    def trend_label(snapshot: TechnicalSnapshot) -> str:
        if snapshot.sma20 is None or snapshot.sma50 is None:
            return "insufficient data"
        if snapshot.close > snapshot.sma20 > snapshot.sma50:
            return "bullish alignment"
        if snapshot.close < snapshot.sma20 < snapshot.sma50:
            return "bearish alignment"
        return "mixed"

    @staticmethod
    def _sma(values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        return fmean(values[-period:])

    @staticmethod
    def _ema(values: list[float], period: int) -> float | None:
        if len(values) < period:
            return None
        current = fmean(values[:period])
        multiplier = 2 / (period + 1)
        for value in values[period:]:
            current = (value - current) * multiplier + current
        return current

    @staticmethod
    def _return_pct(values: list[float], bars_back: int) -> float | None:
        if len(values) <= bars_back or values[-bars_back - 1] == 0:
            return None
        return (values[-1] / values[-bars_back - 1] - 1) * 100

    @staticmethod
    def _rsi(values: list[float], period: int) -> float | None:
        if len(values) <= period:
            return None
        changes = [current - previous for previous, current in zip(values, values[1:])]
        average_gain = fmean(max(change, 0) for change in changes[:period])
        average_loss = fmean(max(-change, 0) for change in changes[:period])
        for change in changes[period:]:
            average_gain = ((average_gain * (period - 1)) + max(change, 0)) / period
            average_loss = ((average_loss * (period - 1)) + max(-change, 0)) / period
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    @staticmethod
    def _atr(bars: list[MarketBar], period: int) -> float | None:
        if len(bars) < period:
            return None
        true_ranges: list[float] = []
        previous_close: float | None = None
        for bar in bars:
            if previous_close is None:
                true_range = bar.high - bar.low
            else:
                true_range = max(
                    bar.high - bar.low,
                    abs(bar.high - previous_close),
                    abs(bar.low - previous_close),
                )
            true_ranges.append(true_range)
            previous_close = bar.close
        current = fmean(true_ranges[:period])
        for true_range in true_ranges[period:]:
            current = ((current * (period - 1)) + true_range) / period
        return current

    @staticmethod
    def _volume_ratio(bars: list[MarketBar], period: int) -> float | None:
        if len(bars) <= period or bars[-1].volume is None:
            return None
        previous_volumes = [
            bar.volume for bar in bars[-period - 1 : -1] if bar.volume is not None
        ]
        if len(previous_volumes) < period:
            return None
        average_volume = fmean(previous_volumes)
        if average_volume <= 0:
            return None
        return bars[-1].volume / average_volume
