from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .utils import normalize_code


class InstrumentType(str, Enum):
    STOCK = "stock"
    INDEX = "index"


class RuleField(str, Enum):
    LAST_PRICE = "last_price"
    CHANGE_PCT = "change_pct"
    OPEN_PRICE = "open_price"
    HIGH_PRICE = "high_price"
    LOW_PRICE = "low_price"
    PREV_CLOSE = "prev_close"
    AMPLITUDE = "amplitude"
    VOLUME = "volume"
    TURNOVER = "turnover"
    TURNOVER_RATE = "turnover_rate"
    PE_TTM = "pe_ttm"
    MA_PRICE = "ma_price"
    MA_CROSS_UP = "ma_cross_up"
    MA_CROSS_DOWN = "ma_cross_down"
    MACD_DIF = "macd_dif"
    MACD_DEA = "macd_dea"
    MACD_HIST = "macd_hist"
    MACD_CROSS_UP = "macd_cross_up"
    MACD_CROSS_DOWN = "macd_cross_down"


class RuleOperator(str, Enum):
    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    NEQ = "neq"


class MonitorBase(BaseModel):
    instrument_type: InstrumentType = InstrumentType.STOCK
    code: str = Field(..., description="代码，例如 600519、000001.SZ、1A0001")
    name: str = Field(default="", max_length=64)
    enabled: bool = True
    note: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def normalize_monitor_code(self) -> "MonitorBase":
        self.code = normalize_code(self.code, self.instrument_type.value)
        return self


class MonitorCreate(MonitorBase):
    pass


class MonitorUpdate(MonitorBase):
    pass


class RuleBase(BaseModel):
    field: RuleField
    operator: RuleOperator
    threshold: float = Field(..., allow_inf_nan=False)
    period: str = Field(default="daily", description="daily / weekly / monthly / yearly / 60m / 30m / 15m")
    ma_period: int = Field(default=20, ge=2, le=500)
    cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    consecutive_hits_required: int = Field(default=1, ge=1, le=100)
    enabled: bool = True
    description: str = Field(default="", max_length=255)

    @field_validator("period")
    @classmethod
    def validate_period(cls, value: str) -> str:
        normalized = (value or "daily").strip().lower()
        if normalized not in {"daily", "weekly", "monthly", "yearly", "60m", "30m", "15m"}:
            raise ValueError("周期只支持 daily / weekly / monthly / yearly / 60m / 30m / 15m")
        return normalized

    @model_validator(mode="after")
    def validate_cross_rule(self) -> "RuleBase":
        if self.field in {RuleField.MACD_CROSS_UP, RuleField.MACD_CROSS_DOWN}:
            self.operator = RuleOperator.EQ
            self.threshold = 1.0
        if self.field in {RuleField.MA_CROSS_UP, RuleField.MA_CROSS_DOWN}:
            self.operator = RuleOperator.EQ
            self.threshold = 1.0
        return self


class RuleCreate(RuleBase):
    pass


class RuleUpdate(RuleBase):
    pass


class RuleOut(RuleBase):
    id: int
    monitor_id: int
    current_consecutive_hits: int = 0
    last_triggered_at: str | None = None
    last_trigger_value: float | None = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class QuoteTarget(BaseModel):
    code: str
    instrument_type: InstrumentType


class QuoteSnapshot(BaseModel):
    code: str
    instrument_type: InstrumentType
    name: str = ""
    last_price: float
    change_pct: float
    change_amount: float = 0.0
    open_price: float
    high_price: float
    low_price: float
    prev_close: float = 0.0
    volume: float
    turnover: float
    turnover_rate: float = 0.0
    pe_ttm: float = 0.0
    timestamp: str
    source: str


class IndicatorOut(BaseModel):
    period: str
    dif: float | None = None
    dea: float | None = None
    histogram: float | None = None
    crossed_up: bool = False
    crossed_down: bool = False
    bars: int = 0
    updated_at: str = ""


class MonitorOut(MonitorBase):
    id: int
    market: str
    created_at: str
    updated_at: str
    rules: list[RuleOut] = Field(default_factory=list)
    latest_quote: QuoteSnapshot | None = None
    indicators: dict[str, IndicatorOut] = Field(default_factory=dict)


class AlertEventOut(BaseModel):
    id: int
    monitor_id: int
    rule_id: int | None
    code: str
    name: str = ""
    message: str
    status: str
    triggered_value: float | None = None
    rule_summary: str = ""
    error: str = ""
    created_at: str

    model_config = ConfigDict(from_attributes=True)


class AlertPageOut(BaseModel):
    items: list[AlertEventOut]
    page: int
    page_size: int
    total: int
    total_pages: int


class UserAuthRecord(BaseModel):
    id: int
    username: str
    password_hash: str
    enabled: bool
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SystemInfo(BaseModel):
    app_name: str
    poll_interval_seconds: int
    quote_provider: str
    market_open: bool = False
    market_status: str = ""
    last_cycle_at: str = ""
    next_cycle_at: str = ""
    monitor_count: int = 0
    alert_count: int = 0
    notification_channel: str = "none"
    notification_configured: bool = False
    data_status: str = "unknown"
    data_message: str = ""
    data_updated_at: str = ""


class PollIntervalUpdate(BaseModel):
    poll_interval_seconds: int = Field(..., ge=60, le=86400)


class NotificationSettings(BaseModel):
    channel: str = Field(default="none", description="none / wecom / bark / webhook")
    webhook_url: str = Field(default="", max_length=2000)
    bark_url: str = Field(default="", max_length=2000)

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        normalized = (value or "none").strip().lower()
        if normalized not in {"none", "wecom", "bark", "webhook"}:
            raise ValueError("通知渠道只支持 none / wecom / bark / webhook")
        return normalized


class NotificationSettingsOut(NotificationSettings):
    configured: bool = False


class TestNotificationRequest(BaseModel):
    channel: str = "wecom"
    webhook_url: str = ""
    bark_url: str = ""


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class AuthRegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r"^\S(?:.*\S)?$")
    password: str = Field(..., min_length=6, max_length=256)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=6, max_length=256)


class AuthLoginResponse(BaseModel):
    authenticated: bool
    username: str = ""
    token: str = ""
    expires_at: str = ""


class AuthStatus(BaseModel):
    authenticated: bool
    username: str = ""
    account_initialized: bool = False
    registration_enabled: bool = True


class ClearAlertsResponse(BaseModel):
    cleared: int


class MetadataItem(BaseModel):
    value: str
    label: str


class MetadataOut(BaseModel):
    instrument_types: list[MetadataItem]
    fields: list[MetadataItem]
    operators: list[MetadataItem]
    periods: list[MetadataItem]


class SystemEventRecord(BaseModel):
    id: int
    event_name: str
    payload: str
    created_at: str

    model_config = ConfigDict(from_attributes=True)


class WebhookJobRecord(BaseModel):
    id: int
    alert_event_id: int
    monitor_id: int
    rule_id: int | None
    code: str
    webhook_url: str
    message: str
    status: str
    attempt_count: int
    max_attempts: int
    available_at: str
    locked_at: str | None = None
    last_error: str = ""
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
