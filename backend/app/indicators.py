from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


class BarLike(Protocol):
    close: float


@dataclass(slots=True)
class MacdState:
    dif: float
    dea: float
    histogram: float
    prev_histogram: float | None
    crossed_up: bool
    crossed_down: bool
    bars_count: int


@dataclass(slots=True)
class MovingAverageState:
    ma5: float
    ma10: float
    ma20: float
    ma60: float | None
    prev_ma5: float
    prev_ma20: float


def _ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(alpha * float(value) + (1.0 - alpha) * result[-1])
    return result


def calculate_macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> list[tuple[float, float, float]]:
    """Return (DIF, DEA, histogram) for every close using the common Chinese MACD convention."""
    if not closes:
        return []
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    dif_values = [fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema)]
    dea_values = _ema(dif_values, signal)
    return [
        (dif_value, dea_value, (dif_value - dea_value) * 2.0)
        for dif_value, dea_value in zip(dif_values, dea_values)
    ]


def macd_state(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> MacdState | None:
    values = calculate_macd(closes, fast=fast, slow=slow, signal=signal)
    if len(values) < max(slow, signal) + 1:
        return None
    dif, dea, histogram = values[-1]
    prev_dif, prev_dea, prev_histogram = values[-2]
    return MacdState(
        dif=dif,
        dea=dea,
        histogram=histogram,
        prev_histogram=prev_histogram,
        crossed_up=prev_dif <= prev_dea and dif > dea,
        crossed_down=prev_dif >= prev_dea and dif < dea,
        bars_count=len(values),
    )


def _sma_at(values: Sequence[float], period: int, end: int) -> float | None:
    if period <= 0 or end + 1 < period:
        return None
    window = values[end + 1 - period : end + 1]
    return sum(window) / period


def moving_average_state(closes: Sequence[float]) -> MovingAverageState | None:
    if len(closes) < 21:
        return None
    ma5 = _sma_at(closes, 5, len(closes) - 1)
    ma10 = _sma_at(closes, 10, len(closes) - 1)
    ma20 = _sma_at(closes, 20, len(closes) - 1)
    ma60 = _sma_at(closes, 60, len(closes) - 1)
    prev_ma5 = _sma_at(closes, 5, len(closes) - 2)
    prev_ma20 = _sma_at(closes, 20, len(closes) - 2)
    if None in (ma5, ma10, ma20, prev_ma5, prev_ma20):
        return None
    return MovingAverageState(
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        ma60=ma60,
        prev_ma5=prev_ma5,
        prev_ma20=prev_ma20,
    )
