from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from .indicators import MacdState, MovingAverageState, macd_state, moving_average_state
from .schemas import InstrumentType, QuoteSnapshot, QuoteTarget
from .utils import eastmoney_secid, normalize_code, quote_cache_key, sina_symbol


logger = logging.getLogger(__name__)
CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class KlineBar:
    time: str
    open: float
    close: float
    high: float
    low: float
    volume: float


@dataclass(slots=True)
class IndicatorState:
    period: str
    macd: MacdState | None
    ma: MovingAverageState | None
    bars: int
    updated_at: str


class QuoteProvider(Protocol):
    async def fetch_many(self, targets: list[QuoteTarget]) -> dict[str, QuoteSnapshot]:
        ...

    async def fetch_indicators(self, targets: list[QuoteTarget], period: str) -> dict[str, IndicatorState]:
        ...


EASTMONEY_FIELDS = "f2,f3,f4,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21,f8,f9,f10,f22,f23,f24,f25,f62,f115,f136,f152"
PERIOD_TO_EASTMONEY = {"daily": 101, "60m": 60, "30m": 30, "15m": 15}
SINA_SCALE = {"daily": 240, "weekly": 1200, "monthly": 7200, "yearly": 7200, "60m": 60, "30m": 30, "15m": 15}
PERIOD_BAR_LIMIT = {"daily": 150, "weekly": 160, "monthly": 160, "yearly": 480, "60m": 150, "30m": 150, "15m": 150}
PERIODS_SUPPORTED = ("daily", "weekly", "monthly", "yearly", "60m", "30m", "15m")


class DualQuoteProvider:
    EASTMONEY_LIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
    SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
    TENCENT_SEARCH_URL = "https://smartbox.gtimg.cn/s3/"

    def __init__(self, batch_size: int = 50) -> None:
        self.batch_size = max(batch_size, 1)

    def _headers(self, referer: str = "https://quote.eastmoney.com/") -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        if value in (None, "", "-"):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _fetch_quotes_batch(self, client: httpx.AsyncClient, batch: list[QuoteTarget]) -> dict[str, QuoteSnapshot]:
        secids = ",".join(eastmoney_secid(target.code, target.instrument_type.value) for target in batch)
        params = {
            "secids": secids,
            "fields": EASTMONEY_FIELDS,
            "fltt": "2",
            "invt": "2",
            "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        }
        response = await client.get(self.EASTMONEY_LIST_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        rows = data.get("diff") or []
        if isinstance(rows, dict):
            rows = list(rows.values())

        target_by_code = {
            normalize_code(target.code, target.instrument_type.value).split(".")[0]: target
            for target in batch
        }
        snapshots: dict[str, QuoteSnapshot] = {}
        for row in rows:
            digits = str(row.get("f12") or "")
            target = target_by_code.get(digits)
            if target is None:
                continue
            normalized = normalize_code(target.code, target.instrument_type.value)
            cache_key = quote_cache_key(normalized, target.instrument_type.value)
            snapshots[cache_key] = QuoteSnapshot(
                code=normalized,
                instrument_type=target.instrument_type,
                name=str(row.get("f14") or digits).strip() or digits,
                last_price=self._to_float(row.get("f2")),
                change_pct=self._to_float(row.get("f3")),
                change_amount=self._to_float(row.get("f4")),
                open_price=self._to_float(row.get("f17")),
                high_price=self._to_float(row.get("f15")),
                low_price=self._to_float(row.get("f16")),
                prev_close=self._to_float(row.get("f18")),
                volume=self._to_float(row.get("f5")),
                turnover=self._to_float(row.get("f6")),
                turnover_rate=self._to_float(row.get("f8")),
                pe_ttm=self._to_float(row.get("f115"), default=0.0),
                timestamp=datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                source="eastmoney",
            )
        return snapshots

    async def _fetch_sina_quotes(self, targets: list[QuoteTarget]) -> dict[str, QuoteSnapshot]:
        if not targets:
            return {}
        symbols = ",".join(sina_symbol(target.code, target.instrument_type.value) for target in targets)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=4.0),
            headers=self._headers("https://finance.sina.com.cn/"),
            trust_env=False,
        ) as client:
            return await self._fetch_sina_quotes_with_client(client, targets, symbols=symbols)

    async def _fetch_sina_quotes_with_client(
        self,
        client: object,
        targets: list[QuoteTarget],
        symbols: str | None = None,
    ) -> dict[str, QuoteSnapshot]:
        symbols = symbols or ",".join(sina_symbol(target.code, target.instrument_type.value) for target in targets)
        response = await client.get(  # type: ignore[attr-defined]
            f"{self.SINA_QUOTE_URL}{symbols}",
            headers=self._headers("https://finance.sina.com.cn/"),
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="ignore")
        target_by_symbol = {
            sina_symbol(target.code, target.instrument_type.value): target
            for target in targets
        }
        snapshots: dict[str, QuoteSnapshot] = {}
        pattern = re.compile(r'^var hq_str_(?P<symbol>[^=]+)="(?P<data>.*)";$')
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            target = target_by_symbol.get(match.group("symbol"))
            if target is None:
                continue
            parts = match.group("data").split(",")
            if len(parts) < 10:
                continue
            prev_close = self._to_float(parts[2])
            last_price = self._to_float(parts[3])
            normalized = normalize_code(target.code, target.instrument_type.value)
            snapshots[quote_cache_key(normalized, target.instrument_type.value)] = QuoteSnapshot(
                code=normalized,
                instrument_type=target.instrument_type,
                name=parts[0].strip() or normalized,
                last_price=last_price,
                change_pct=((last_price - prev_close) / prev_close * 100.0) if prev_close else 0.0,
                change_amount=last_price - prev_close,
                open_price=self._to_float(parts[1]),
                high_price=self._to_float(parts[4]),
                low_price=self._to_float(parts[5]),
                prev_close=prev_close,
                volume=self._to_float(parts[8]) / 100.0,
                turnover=self._to_float(parts[9]),
                turnover_rate=0.0,
                pe_ttm=0.0,
                timestamp=f"{parts[-3]} {parts[-2]}" if len(parts) >= 3 else datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                source="sina",
            )
        return snapshots

    async def _fetch_tencent_quotes(self, targets: list[QuoteTarget]) -> dict[str, QuoteSnapshot]:
        if not targets:
            return {}
        symbols = ",".join(sina_symbol(target.code, target.instrument_type.value) for target in targets)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers=self._headers("https://gu.qq.com/"),
            trust_env=False,
        ) as client:
            return await self._fetch_tencent_quotes_with_client(client, targets, symbols=symbols)

    async def _fetch_tencent_quotes_with_client(
        self,
        client: object,
        targets: list[QuoteTarget],
        symbols: str | None = None,
    ) -> dict[str, QuoteSnapshot]:
        symbols = symbols or ",".join(sina_symbol(target.code, target.instrument_type.value) for target in targets)
        response = await client.get(  # type: ignore[attr-defined]
            f"{self.TENCENT_QUOTE_URL}{symbols}",
            headers=self._headers("https://gu.qq.com/"),
        )
        response.raise_for_status()
        text = response.content.decode("gbk", errors="ignore")
        target_by_symbol = {
            sina_symbol(target.code, target.instrument_type.value): target
            for target in targets
        }
        snapshots: dict[str, QuoteSnapshot] = {}
        pattern = re.compile(r'^v_(?P<symbol>[^=]+)="(?P<data>.*)";?$')
        for line in text.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            target = target_by_symbol.get(match.group("symbol"))
            if target is None:
                continue
            parts = match.group("data").split("~")
            if len(parts) < 35:
                continue
            last_price = self._to_float(parts[3])
            prev_close = self._to_float(parts[4])
            normalized = normalize_code(target.code, target.instrument_type.value)
            timestamp = parts[30].strip()
            if len(timestamp) == 14 and timestamp.isdigit():
                timestamp = (
                    f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]} "
                    f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}"
                )
            else:
                timestamp = datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
            snapshots[quote_cache_key(normalized, target.instrument_type.value)] = QuoteSnapshot(
                code=normalized,
                instrument_type=target.instrument_type,
                name=parts[1].strip() or normalized,
                last_price=last_price,
                change_pct=self._to_float(parts[32]),
                change_amount=self._to_float(parts[31]),
                open_price=self._to_float(parts[5]),
                high_price=self._to_float(parts[33]),
                low_price=self._to_float(parts[34]),
                prev_close=prev_close,
                volume=self._to_float(parts[6]),
                turnover=self._to_float(parts[35].split("/")[2]) if len(parts) > 35 and parts[35].count("/") >= 2 else 0.0,
                turnover_rate=self._to_float(parts[38]) if len(parts) > 38 else 0.0,
                pe_ttm=self._to_float(parts[39]) if len(parts) > 39 else 0.0,
                timestamp=timestamp,
                source="tencent",
            )
        return snapshots

    async def fetch_many(self, targets: list[QuoteTarget]) -> dict[str, QuoteSnapshot]:
        if not targets:
            return {}
        unique: dict[str, QuoteTarget] = {}
        for target in targets:
            key = quote_cache_key(target.code, target.instrument_type.value)
            unique.setdefault(key, target)
        ordered = list(unique.values())
        snapshots: dict[str, QuoteSnapshot] = {}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers=self._headers(),
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as client:
            for start in range(0, len(ordered), self.batch_size):
                batch = ordered[start : start + self.batch_size]
                try:
                    snapshots.update(await self._fetch_quotes_batch(client, batch))
                except Exception as exc:
                    logger.warning("Eastmoney batch quote failed (%s targets): %s", len(batch), exc)

        for fallback_name, fallback in (
            ("Sina", self._fetch_sina_quotes),
            ("Tencent", self._fetch_tencent_quotes),
        ):
            missing = [target for key, target in unique.items() if key not in snapshots]
            if not missing:
                break
            try:
                snapshots.update(await fallback(missing))
            except Exception as exc:
                logger.warning("%s fallback quote failed (%s targets): %s", fallback_name, len(missing), exc)
        return snapshots

    async def _fetch_eastmoney_kline(self, client: httpx.AsyncClient, target: QuoteTarget, period: str) -> list[KlineBar]:
        klt = PERIOD_TO_EASTMONEY.get(period, 101)
        params = {
            "secid": eastmoney_secid(target.code, target.instrument_type.value),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": str(klt),
            "fqt": "1",
            "end": "20500101",
            "lmt": "150",
        }
        response = await client.get(self.EASTMONEY_KLINE_URL, params=params)
        response.raise_for_status()
        data = (response.json().get("data") or {})
        bars: list[KlineBar] = []
        for raw_line in data.get("klines") or []:
            parts = raw_line.split(",")
            if len(parts) < 7:
                continue
            bars.append(
                KlineBar(
                    time=parts[0],
                    open=self._to_float(parts[1]),
                    close=self._to_float(parts[2]),
                    high=self._to_float(parts[3]),
                    low=self._to_float(parts[4]),
                    volume=self._to_float(parts[5]),
                )
            )
        return bars

    async def _fetch_sina_kline(self, client: httpx.AsyncClient, target: QuoteTarget, period: str) -> list[KlineBar]:
        normalized = normalize_code(target.code, target.instrument_type.value)
        params = {
            "symbol": sina_symbol(normalized, target.instrument_type.value),
            "scale": str(SINA_SCALE.get(period, 240)),
            "ma": "no",
            "datalen": str(PERIOD_BAR_LIMIT.get(period, 150)),
        }
        response = await client.get(self.SINA_KLINE_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []
        return [
            KlineBar(
                time=str(item.get("day") or ""),
                open=self._to_float(item.get("open")),
                close=self._to_float(item.get("close")),
                high=self._to_float(item.get("high")),
                low=self._to_float(item.get("low")),
                volume=self._to_float(item.get("volume")) / 100.0,
            )
            for item in payload
            if isinstance(item, dict)
        ]

    async def search_instruments(self, keyword: str, limit: int = 8) -> list[dict[str, str]]:
        query = (keyword or "").strip()
        if not query:
            return []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            headers=self._headers("https://gu.qq.com/"),
            trust_env=False,
        ) as client:
            response = await client.get(self.TENCENT_SEARCH_URL, params={"q": query, "t": "all"})
            response.raise_for_status()
            text = response.content.decode("gbk", errors="ignore")
        return self._parse_search_response(text, limit)

    @staticmethod
    def _parse_search_response(text: str, limit: int = 8) -> list[dict[str, str]]:
        matched = re.search(r'v_hint="(?P<data>.*)"', text)
        if not matched:
            return []
        results: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in matched.group("data").split("^"):
            parts = item.split("~")
            if len(parts) < 5:
                continue
            market, code, name, _pinyin, kind = parts[:5]
            if kind not in {"GP-A", "ZS"}:
                continue
            if "\\u" in name:
                name = name.encode("latin-1", errors="ignore").decode("unicode_escape")
            instrument_type = "index" if kind == "ZS" else "stock"
            try:
                normalized = normalize_code(code, instrument_type)
            except ValueError:
                continue
            key = (instrument_type, normalized)
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "code": normalized,
                    "name": name,
                    "instrument_type": instrument_type,
                }
            )
            if len(results) >= limit:
                break
        return results

    async def fetch_indicators(self, targets: list[QuoteTarget], period: str) -> dict[str, IndicatorState]:
        if not targets:
            return {}
        unique: dict[str, QuoteTarget] = {}
        for target in targets:
            unique.setdefault(quote_cache_key(target.code, target.instrument_type.value), target)

        semaphore = asyncio.Semaphore(3)
        states: dict[str, IndicatorState] = {}
        headers = self._headers()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers=headers,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as client:
            async def fetch_one(key: str, target: QuoteTarget) -> tuple[str, IndicatorState | None]:
                async with semaphore:
                    bars: list[KlineBar] = []
                    if period in {"weekly", "monthly", "yearly"}:
                        try:
                            bars = await self._fetch_sina_kline(client, target, period)
                        except Exception as exc:
                            logger.warning("Sina kline failed for %s period=%s: %s", key, period, exc)
                        if period == "yearly":
                            bars = self._aggregate_bars(bars, 12)
                    else:
                        try:
                            bars = await self._fetch_eastmoney_kline(client, target, period)
                        except Exception as exc:
                            logger.warning("Eastmoney kline failed for %s: %s", key, exc)
                    if len(bars) < 35:
                        if period not in {"weekly", "monthly", "yearly"}:
                            try:
                                bars = await self._fetch_sina_kline(client, target, period)
                            except Exception as exc:
                                logger.warning("Sina kline fallback failed for %s: %s", key, exc)
                    if not bars:
                        return key, None
                    closes = [bar.close for bar in bars]
                    return key, IndicatorState(
                        period=period,
                        macd=macd_state(closes),
                        ma=moving_average_state(closes),
                        bars=len(bars),
                        updated_at=datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    )

            results = await asyncio.gather(*(fetch_one(key, target) for key, target in unique.items()))
        for key, state in results:
            if state is not None:
                states[key] = state
        return states

    @staticmethod
    def _aggregate_bars(bars: list[KlineBar], size: int) -> list[KlineBar]:
        aggregated: list[KlineBar] = []
        for start in range(0, len(bars), size):
            chunk = bars[start : start + size]
            if not chunk:
                continue
            aggregated.append(
                KlineBar(
                    time=chunk[-1].time,
                    open=chunk[0].open,
                    close=chunk[-1].close,
                    high=max(bar.high for bar in chunk),
                    low=min(bar.low for bar in chunk),
                    volume=sum(bar.volume for bar in chunk),
                )
            )
        return aggregated


def build_quote_provider(name: str = "eastmoney") -> QuoteProvider:
    if name.strip().lower() not in {"eastmoney", "eastmoney-dual"}:
        logger.warning("Ignoring unsupported quote provider '%s'; using Eastmoney + Sina.", name)
    return DualQuoteProvider()
