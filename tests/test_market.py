from datetime import datetime
from zoneinfo import ZoneInfo

from backend.app.market import market_open, market_phase
from backend.app.schemas import RuleCreate, RuleField, RuleOperator


CHINA = ZoneInfo("Asia/Shanghai")


def test_market_is_open_on_weekday_session():
    assert market_open(datetime(2026, 11, 16, 10, 0, tzinfo=CHINA))


def test_lunch_is_not_open():
    assert market_phase(datetime(2026, 11, 16, 12, 0, tzinfo=CHINA)) == "lunch"


def test_weekend_is_closed():
    assert not market_open(datetime(2026, 11, 15, 10, 0, tzinfo=CHINA))


def test_periods_accept_weekly_monthly_yearly():
    for period in ("weekly", "monthly", "yearly"):
        rule = RuleCreate(field=RuleField.MACD_HIST, operator=RuleOperator.GTE, threshold=0, period=period)
        assert rule.period == period


def test_ma_price_rule_accepts_ma_period():
    rule = RuleCreate(field=RuleField.MA_PRICE, operator=RuleOperator.GTE, threshold=10, period="weekly", ma_period=20)
    assert rule.ma_period == 20
