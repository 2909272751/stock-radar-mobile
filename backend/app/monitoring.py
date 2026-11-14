from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from .market import market_open, market_status_text, next_market_open, now_local
from .quote_provider import IndicatorState, QuoteProvider
from .repository import MonitorRepository
from .schemas import MonitorOut, QuoteSnapshot, QuoteTarget, RuleField, RuleOperator, RuleOut
from .utils import parse_iso, quote_cache_key


logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "last_price": "最新价",
    "change_pct": "涨跌幅",
    "open_price": "开盘价",
    "high_price": "最高价",
    "low_price": "最低价",
    "prev_close": "昨收",
    "amplitude": "振幅",
    "volume": "成交量",
    "turnover": "成交额",
    "turnover_rate": "换手率",
    "pe_ttm": "市盈率TTM",
    "ma_price": "均线价格",
    "ma_cross_up": "均线金叉",
    "ma_cross_down": "均线死叉",
    "macd_dif": "MACD DIF",
    "macd_dea": "MACD DEA",
    "macd_hist": "MACD 柱",
    "macd_cross_up": "MACD 金叉",
    "macd_cross_down": "MACD 死叉",
}

OPERATOR_LABELS = {
    "gte": "≥",
    "lte": "≤",
    "eq": "=",
    "neq": "≠",
}

PERIOD_LABELS = {
    "daily": "日线",
    "weekly": "周线",
    "monthly": "月线",
    "yearly": "年线",
    "60m": "60 分钟",
    "30m": "30 分钟",
    "15m": "15 分钟",
}

INDICATOR_FIELDS = {
    RuleField.MA_PRICE,
    RuleField.MA_CROSS_UP,
    RuleField.MA_CROSS_DOWN,
    RuleField.MACD_DIF,
    RuleField.MACD_DEA,
    RuleField.MACD_HIST,
    RuleField.MACD_CROSS_UP,
    RuleField.MACD_CROSS_DOWN,
}


@dataclass(slots=True)
class CycleResult:
    processed: int = 0
    matched: int = 0
    queued: int = 0
    failed: int = 0
    skipped: bool = False
    message: str = ""


def compare_value(operator: RuleOperator, actual: float, threshold: float) -> bool:
    if operator == RuleOperator.GTE:
        return actual >= threshold
    if operator == RuleOperator.LTE:
        return actual <= threshold
    if operator == RuleOperator.EQ:
        return abs(actual - threshold) < 1e-9
    if operator == RuleOperator.NEQ:
        return abs(actual - threshold) >= 1e-9
    return False


def cooldown_ready(rule: RuleOut) -> bool:
    if rule.cooldown_minutes == 0 or not rule.last_triggered_at:
        return True
    last_triggered = parse_iso(rule.last_triggered_at)
    if last_triggered is None:
        return True
    return now_local() - last_triggered >= timedelta(minutes=rule.cooldown_minutes)


def advance_hit_state(matched: bool, current_hits: int, required_hits: int) -> tuple[int, bool]:
    required = max(int(required_hits or 1), 1)
    current = max(int(current_hits or 0), 0)
    if not matched:
        return 0, False
    if current >= required:
        return required + 1, False
    next_hits = min(current + 1, required)
    return next_hits, next_hits >= required


def _indicator_value(field: RuleField, indicators: dict[str, IndicatorState]) -> float | None:
    period = ""
    for state in indicators.values():
        period = state.period
        macd = state.macd
        if macd is None:
            return None
        if field == RuleField.MACD_DIF:
            return macd.dif
        if field == RuleField.MACD_DEA:
            return macd.dea
        if field == RuleField.MACD_HIST:
            return macd.histogram
        if field == RuleField.MACD_CROSS_UP:
            return 1.0 if macd.crossed_up else 0.0
        if field == RuleField.MACD_CROSS_DOWN:
            return 1.0 if macd.crossed_down else 0.0
    del period
    return None


def build_alert_text(monitor: MonitorOut, rule: RuleOut, quote: QuoteSnapshot, actual: float) -> tuple[str, str]:
    field_label = FIELD_LABELS.get(rule.field.value, rule.field.value)
    period_label = PERIOD_LABELS.get(rule.period, rule.period)
    display_name = monitor.name or quote.name or monitor.code
    if rule.field in {RuleField.MACD_CROSS_UP, RuleField.MACD_CROSS_DOWN}:
        direction = "金叉" if rule.field == RuleField.MACD_CROSS_UP else "死叉"
        headline = f"{display_name} MACD {period_label}出现{direction}"
        detail = f"{period_label} DIF 已{'上穿' if direction == '金叉' else '下穿'} DEA"
    elif rule.field in {RuleField.MA_CROSS_UP, RuleField.MA_CROSS_DOWN}:
        direction = "金叉" if rule.field == RuleField.MA_CROSS_UP else "死叉"
        headline = f"{display_name} 均线 {period_label}出现{direction}"
        detail = f"MA5 已{'上穿' if direction == '金叉' else '下穿'} MA20"
    elif rule.field == RuleField.MA_PRICE:
        operator_label = OPERATOR_LABELS.get(rule.operator.value, rule.operator.value)
        headline = f"{display_name} MA{rule.ma_period}{operator_label}{rule.threshold:g} 已触发"
        detail = f"MA{rule.ma_period} 当前 {actual:.4g}，条件 {operator_label} {rule.threshold:g}"
    else:
        operator_label = OPERATOR_LABELS.get(rule.operator.value, rule.operator.value)
        headline = f"{display_name} {field_label}{operator_label}{rule.threshold:g} 已触发"
        detail = f"{field_label} 当前 {actual:.4g}，条件 {operator_label} {rule.threshold:g}"
    body = (
        f"{headline}\n"
        f"{detail}\n"
        f"最新价 {quote.last_price:.2f}，涨跌 {quote.change_pct:+.2f}%\n"
        f"行情时间 {quote.timestamp}\n"
        f"规则备注 {rule.description or '无'}"
    )
    return headline, body


class MonitorService:
    def __init__(self, repository: MonitorRepository, quote_provider: QuoteProvider, poll_interval_seconds: int) -> None:
        self.repository = repository
        self.quote_provider = quote_provider
        self.poll_interval_seconds = max(poll_interval_seconds, 60)
        self.last_cycle_at = ""
        self.next_cycle_at = ""
        self.last_data_status = "unknown"
        self.last_data_message = ""
        self.last_result = CycleResult(skipped=True, message="等待下一次检查")
        self._quote_refresh_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            await self._task

    async def _reload_poll_interval(self) -> None:
        saved_value = await asyncio.to_thread(self.repository.get_app_setting, "poll_interval_seconds")
        if not saved_value:
            return
        try:
            self.poll_interval_seconds = max(int(saved_value), 60)
        except ValueError:
            logger.warning("Ignored invalid poll interval: %s", saved_value)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._reload_poll_interval()
                await self.run_cycle()
            except Exception:
                logger.exception("Monitoring cycle failed")
            self.next_cycle_at = (now_local() + timedelta(seconds=self.poll_interval_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            await asyncio.to_thread(self.repository.set_app_setting, "next_cycle_at", self.next_cycle_at)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def _fetch_quotes(self, monitors: list[MonitorOut], publish_event: bool = True) -> dict[str, QuoteSnapshot]:
        if not monitors:
            return {}
        async with self._quote_refresh_lock:
            targets_by_key: dict[str, QuoteTarget] = {}
            for monitor in monitors:
                key = quote_cache_key(monitor.code, monitor.instrument_type.value)
                targets_by_key.setdefault(key, QuoteTarget(code=monitor.code, instrument_type=monitor.instrument_type))
            quotes = await self.quote_provider.fetch_many(list(targets_by_key.values()))
            await asyncio.to_thread(self.repository.upsert_quotes, quotes)
            if publish_event and quotes:
                await asyncio.to_thread(
                    self.repository.create_system_event,
                    "quotes_updated",
                    {"updated": len(quotes), "latest_quote_time": max(quote.timestamp for quote in quotes.values())},
                )
            return quotes

    async def _refresh_indicators(self, monitors: list[MonitorOut]) -> dict[str, dict[str, IndicatorState]]:
        periods_by_key: dict[str, set[str]] = {}
        target_by_key: dict[str, QuoteTarget] = {}
        for monitor in monitors:
            key = quote_cache_key(monitor.code, monitor.instrument_type.value)
            target_by_key[key] = QuoteTarget(code=monitor.code, instrument_type=monitor.instrument_type)
            for rule in monitor.rules:
                if rule.enabled and rule.field in INDICATOR_FIELDS:
                    periods_by_key.setdefault(key, set()).add(rule.period)
        result: dict[str, dict[str, IndicatorState]] = {}
        for period in {period for periods in periods_by_key.values() for period in periods}:
            keys = [key for key, periods in periods_by_key.items() if period in periods]
            states = await self.quote_provider.fetch_indicators([target_by_key[key] for key in keys], period)
            for key, state in states.items():
                result.setdefault(key, {})[period] = state
                await asyncio.to_thread(self.repository.upsert_indicators, key, period, state)
        return result

    def _rule_actual(self, rule: RuleOut, quote: QuoteSnapshot, indicators: dict[str, IndicatorState]) -> float | None:
        if rule.field == RuleField.LAST_PRICE:
            return quote.last_price
        if rule.field == RuleField.CHANGE_PCT:
            return quote.change_pct
        if rule.field == RuleField.OPEN_PRICE:
            return quote.open_price
        if rule.field == RuleField.HIGH_PRICE:
            return quote.high_price
        if rule.field == RuleField.LOW_PRICE:
            return quote.low_price
        if rule.field == RuleField.PREV_CLOSE:
            return quote.prev_close
        if rule.field == RuleField.AMPLITUDE:
            return ((quote.high_price - quote.low_price) / quote.prev_close * 100.0) if quote.prev_close else 0.0
        if rule.field == RuleField.VOLUME:
            return quote.volume
        if rule.field == RuleField.TURNOVER:
            return quote.turnover
        if rule.field == RuleField.TURNOVER_RATE:
            return quote.turnover_rate
        if rule.field == RuleField.PE_TTM:
            return quote.pe_ttm
        state = indicators.get(rule.period)
        if state is None:
            return None
        if rule.field == RuleField.MA_PRICE:
            if state.ma is None:
                return None
            return {
                5: state.ma.ma5,
                10: state.ma.ma10,
                20: state.ma.ma20,
                60: state.ma.ma60,
            }.get(rule.ma_period)
        if rule.field == RuleField.MA_CROSS_UP:
            return 1.0 if state.ma and state.ma.prev_ma5 <= state.ma.prev_ma20 and state.ma.ma5 > state.ma.ma20 else 0.0
        if rule.field == RuleField.MA_CROSS_DOWN:
            return 1.0 if state.ma and state.ma.prev_ma5 >= state.ma.prev_ma20 and state.ma.ma5 < state.ma.ma20 else 0.0
        if state.macd is None:
            return None
        if rule.field == RuleField.MACD_DIF:
            return state.macd.dif
        if rule.field == RuleField.MACD_DEA:
            return state.macd.dea
        if rule.field == RuleField.MACD_HIST:
            return state.macd.histogram
        if rule.field == RuleField.MACD_CROSS_UP:
            return 1.0 if state.macd.crossed_up else 0.0
        if rule.field == RuleField.MACD_CROSS_DOWN:
            return 1.0 if state.macd.crossed_down else 0.0
        return None

    async def _evaluate(self, monitor: MonitorOut, quote: QuoteSnapshot | None, indicators: dict[str, IndicatorState]) -> CycleResult:
        summary = CycleResult(processed=1)
        if quote is None:
            summary.message = "行情为空"
            return summary
        for rule in monitor.rules:
            if not rule.enabled:
                continue
            actual = self._rule_actual(rule, quote, indicators)
            if actual is None:
                await asyncio.to_thread(self.repository.set_rule_hits, rule.id, 0)
                continue
            matched = compare_value(rule.operator, actual, rule.threshold)
            if rule.field in {RuleField.MACD_CROSS_UP, RuleField.MACD_CROSS_DOWN}:
                matched = actual >= 0.5
            if rule.field in {RuleField.MA_CROSS_UP, RuleField.MA_CROSS_DOWN}:
                matched = actual >= 0.5
            if matched:
                summary.matched += 1
            new_hits, should_trigger = advance_hit_state(
                matched,
                rule.current_consecutive_hits,
                rule.consecutive_hits_required,
            )
            if not should_trigger:
                if new_hits != rule.current_consecutive_hits:
                    await asyncio.to_thread(self.repository.set_rule_hits, rule.id, new_hits)
                continue
            if not cooldown_ready(rule):
                await asyncio.to_thread(self.repository.set_rule_hits, rule.id, rule.consecutive_hits_required + 1)
                continue
            claimed = await asyncio.to_thread(
                self.repository.claim_rule_trigger,
                rule.id,
                rule.current_consecutive_hits,
                rule.consecutive_hits_required + 1,
            )
            if not claimed:
                continue
            title, message = build_alert_text(monitor, rule, quote, actual)
            rule_summary = (
                f"{PERIOD_LABELS.get(rule.period, rule.period)} MA{rule.ma_period} "
                f"{OPERATOR_LABELS.get(rule.operator.value, '')} {rule.threshold:g}"
                if rule.field == RuleField.MA_PRICE
                else f"{FIELD_LABELS.get(rule.field.value, rule.field.value)} "
                f"{OPERATOR_LABELS.get(rule.operator.value, '')} {rule.threshold:g}"
            )
            try:
                await asyncio.to_thread(
                    self.repository.queue_alert,
                    monitor.id,
                    rule.id,
                    monitor.code,
                    monitor.name or quote.name,
                    title,
                    message,
                    actual,
                    rule_summary,
                )
                await asyncio.to_thread(self.repository.mark_rule_triggered, rule.id, actual, now_local().isoformat(timespec="seconds"))
                summary.queued += 1
            except Exception:
                logger.exception("Failed to queue alert monitor=%s rule=%s", monitor.id, rule.id)
                await asyncio.to_thread(
                    self.repository.set_rule_hits,
                    rule.id,
                    max(rule.consecutive_hits_required - 1, 0),
                )
                summary.failed += 1
        return summary

    async def _record_last_cycle(self) -> None:
        self.last_cycle_at = now_local().strftime("%Y-%m-%d %H:%M:%S")
        await asyncio.to_thread(self.repository.set_app_setting, "last_cycle_at", self.last_cycle_at)

    async def _persist_data_status(self) -> None:
        await asyncio.to_thread(self.repository.set_app_setting, "data_status", self.last_data_status)
        await asyncio.to_thread(self.repository.set_app_setting, "data_message", self.last_data_message)

    async def run_cycle(self, user_id: int | None = None, force: bool = False) -> CycleResult:
        monitors = await asyncio.to_thread(self.repository.list_enabled_monitors, user_id)
        if not monitors:
            self.last_result = CycleResult(skipped=True, message="还没有启用的监控")
            await self._record_last_cycle()
            return self.last_result
        if not force and not market_open():
            self.last_result = CycleResult(skipped=True, message=f"{market_status_text()}，暂停行情检查")
            await self._record_last_cycle()
            return self.last_result
        quotes = await self._fetch_quotes(monitors)
        expected_keys = {quote_cache_key(monitor.code, monitor.instrument_type.value) for monitor in monitors}
        missing_monitors = [monitor for monitor in monitors if quote_cache_key(monitor.code, monitor.instrument_type.value) not in quotes]
        if not quotes:
            self.last_data_status = "error"
            self.last_data_message = "行情源全部请求失败：东方财富、新浪、腾讯均未返回数据，请检查网络或行情接口"
            await self._alert_data_error(user_id, monitors, self.last_data_message)
        elif missing_monitors:
            self.last_data_status = "partial"
            names = "、".join(monitor.name or monitor.code for monitor in missing_monitors[:5])
            self.last_data_message = f"部分股票行情缺失：{names}；其余 {len(quotes)} 只已更新"
            await self._alert_data_error(user_id, missing_monitors, self.last_data_message)
        else:
            self.last_data_status = "ok"
            self.last_data_message = ""
            if await asyncio.to_thread(self.repository.get_app_setting, "data_error_alerted_at"):
                await asyncio.to_thread(self.repository.set_app_setting, "data_error_alerted_at", "")
        await self._persist_data_status()
        indicators = await self._refresh_indicators(monitors)
        result = CycleResult()
        if not quotes:
            result.failed = len(expected_keys)
        for monitor in monitors:
            key = quote_cache_key(monitor.code, monitor.instrument_type.value)
            try:
                item = await self._evaluate(monitor, quotes.get(key), indicators.get(key, {}))
                result.processed += item.processed
                result.matched += item.matched
                result.queued += item.queued
                result.failed += item.failed
            except Exception:
                logger.exception("Monitor evaluation failed monitor=%s", monitor.id)
                result.failed += 1
        if not quotes:
            result.message = "检查失败：行情源没有返回数据，请查看状态说明"
        elif missing_monitors:
            result.message = f"检查 {result.processed} 只，{len(missing_monitors)} 只行情缺失，命中 {result.matched} 条，新增提醒 {result.queued} 条"
        else:
            result.message = f"检查 {result.processed} 只，命中 {result.matched} 条，新增提醒 {result.queued} 条"
        self.last_result = result
        await self._record_last_cycle()
        await asyncio.to_thread(self.repository.create_system_event, "cycle_completed", asdict(result))
        return result

    async def _alert_data_error(self, user_id: int | None, monitors: list[MonitorOut], message: str) -> None:
        now = now_local()
        last_alert_at = await asyncio.to_thread(self.repository.get_app_setting, "data_error_alerted_at")
        if last_alert_at:
            try:
                last = parse_iso(last_alert_at)
                if last and (now - last) < timedelta(minutes=60):
                    return
            except ValueError:
                pass
        await asyncio.to_thread(
            self.repository.create_system_alert,
            user_id,
            "股价雷达行情数据异常",
            f"{message}\n时间 {now.strftime('%Y-%m-%d %H:%M:%S')}\n监控 {len(monitors)} 只，请检查手机网络或行情接口。",
            "DATA",
            "数据源告警",
        )
        await asyncio.to_thread(self.repository.set_app_setting, "data_error_alerted_at", now.isoformat(timespec="seconds"))
        await asyncio.to_thread(self.repository.create_system_event, "data_error", {"message": message, "monitor_count": len(monitors)})

    async def refresh_quotes_for_all(self, user_id: int | None = None) -> dict[str, int]:
        monitors = await asyncio.to_thread(self.repository.list_monitors, user_id)
        quotes = await self._fetch_quotes(monitors)
        return {"updated": len(quotes)}

    async def list_monitors_with_quotes(self, monitors: list[MonitorOut] | None = None, user_id: int | None = None) -> list[MonitorOut]:
        if monitors is None:
            monitors = await asyncio.to_thread(self.repository.list_monitors, user_id)
        quote_map = await asyncio.to_thread(self.repository.get_quote_map)
        indicator_map = await asyncio.to_thread(self.repository.get_indicator_map)
        enriched: list[MonitorOut] = []
        for monitor in monitors:
            key = quote_cache_key(monitor.code, monitor.instrument_type.value)
            indicators = {
                period: value
                for (cache_key, period), value in indicator_map.items()
                if cache_key == key
            }
            enriched.append(monitor.model_copy(update={"latest_quote": quote_map.get(key), "indicators": indicators}))
        return enriched

    def system_status(self) -> dict[str, object]:
        return {
            "market_open": market_open(),
            "market_status": market_status_text(),
            "last_cycle_at": self.last_cycle_at,
            "next_cycle_at": self.next_cycle_at,
            "last_result": self.last_result.message,
        }
