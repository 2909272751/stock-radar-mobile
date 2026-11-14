import asyncio
from pathlib import Path

from backend.app.database import Database
from backend.app.monitoring import MonitorService, advance_hit_state
from backend.app.repository import MonitorRepository
from backend.app.schemas import (
    InstrumentType,
    MonitorCreate,
    QuoteSnapshot,
    RuleCreate,
    RuleField,
    RuleOperator,
)


class DummyQuoteProvider:
    async def fetch_many(self, targets):
        return {}

    async def fetch_indicators(self, targets, period):
        return {}


def make_quote(price: float) -> QuoteSnapshot:
    return QuoteSnapshot(
        code="600519.SH",
        instrument_type=InstrumentType.STOCK,
        name="测试股票",
        last_price=price,
        change_pct=1.0,
        open_price=price,
        high_price=price,
        low_price=price,
        prev_close=price,
        volume=1000,
        turnover=100000,
        turnover_rate=1.0,
        timestamp="2026-11-14 10:00:00",
        source="test",
    )


def build_service(tmp_path: Path, *, required: int = 1, cooldown: int = 0):
    database = Database(tmp_path / "test.db")
    database.init_db()
    repository = MonitorRepository(database)
    user = repository.create_user("tester", "hash")
    monitor = repository.create_monitor(MonitorCreate(code="600519", name="测试股票"), user.id)
    repository.create_rule(
        monitor.id,
        RuleCreate(
            field=RuleField.LAST_PRICE,
            operator=RuleOperator.GTE,
            threshold=100,
            cooldown_minutes=cooldown,
            consecutive_hits_required=required,
        ),
        user.id,
    )
    service = MonitorService(repository, DummyQuoteProvider(), 900)
    return service, repository, user.id, monitor.id


def test_duplicate_rule_submission_returns_existing_rule(tmp_path):
    database = Database(tmp_path / "duplicate.db")
    database.init_db()
    repository = MonitorRepository(database)
    user = repository.create_user("duplicate-tester", "hash")
    monitor = repository.create_monitor(MonitorCreate(code="600519", name="贵州茅台"), user.id)
    payload = RuleCreate(
        field=RuleField.LAST_PRICE,
        operator=RuleOperator.GTE,
        threshold=1500,
        cooldown_minutes=30,
    )

    first = repository.create_rule(monitor.id, payload, user.id)
    second = repository.create_rule(monitor.id, payload, user.id)

    assert first.id == second.id
    stored = repository.get_monitor(monitor.id, user.id)
    assert len(stored.rules) == 1


def test_hit_state_requires_consecutive_matches():
    assert advance_hit_state(True, 0, 3) == (1, False)
    assert advance_hit_state(True, 1, 3) == (2, False)
    assert advance_hit_state(True, 2, 3) == (3, True)
    assert advance_hit_state(True, 3, 3) == (4, False)
    assert advance_hit_state(False, 3, 3) == (0, False)


def test_continuous_condition_alerts_only_once_then_rearms(tmp_path):
    async def scenario():
        service, repository, user_id, monitor_id = build_service(tmp_path)

        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(150), {})
        alerts, total = repository.list_alerts(user_id=user_id)
        assert total == 1

        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(151), {})
        _, total = repository.list_alerts(user_id=user_id)
        assert total == 1

        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(90), {})
        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(152), {})
        _, total = repository.list_alerts(user_id=user_id)
        assert total == 2

    asyncio.run(scenario())


def test_required_consecutive_hits_triggers_on_third_match(tmp_path):
    async def scenario():
        service, repository, user_id, monitor_id = build_service(tmp_path, required=3)

        for expected in (0, 0, 1, 1):
            monitor = repository.get_monitor(monitor_id, user_id)
            await service._evaluate(monitor, make_quote(150), {})
            _, total = repository.list_alerts(user_id=user_id)
            assert total == expected

    asyncio.run(scenario())


def test_cooldown_consumes_signal_without_deferred_alert(tmp_path):
    async def scenario():
        service, repository, user_id, monitor_id = build_service(tmp_path, cooldown=30)

        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(150), {})
        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(90), {})
        monitor = repository.get_monitor(monitor_id, user_id)
        await service._evaluate(monitor, make_quote(151), {})
        _, total = repository.list_alerts(user_id=user_id)
        assert total == 1

    asyncio.run(scenario())


def test_all_quote_sources_empty_creates_one_data_alert_and_recovers(tmp_path):
    async def scenario():
        service, repository, user_id, _ = build_service(tmp_path)

        first = await service.run_cycle(force=True)
        assert first.processed == 1
        assert first.failed == 1
        alerts, total = repository.list_alerts(user_id=user_id)
        assert total == 1
        assert alerts[0].code == "DATA"

        second = await service.run_cycle(force=True)
        assert second.processed == 1
        _, total = repository.list_alerts(user_id=user_id)
        assert total == 1

    asyncio.run(scenario())


def test_successful_cycle_clears_data_error_dedupe(tmp_path):
    async def scenario():
        service, repository, user_id, _ = build_service(tmp_path)
        await service.run_cycle(force=True)
        assert repository.get_app_setting("data_error_alerted_at")

        class WorkingProvider:
            async def fetch_many(self, targets):
                return {"stock:600519.SH": make_quote(90)}

            async def fetch_indicators(self, targets, period):
                return {}

        service.quote_provider = WorkingProvider()
        await service.run_cycle(force=True)
        assert repository.get_app_setting("data_error_alerted_at") == ""
        assert repository.get_app_setting("data_status") == "ok"

    asyncio.run(scenario())
