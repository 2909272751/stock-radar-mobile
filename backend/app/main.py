from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import LoginAuthManager
from .config import Settings
from .database import Database
from .market import market_open, market_status_text
from .monitoring import FIELD_LABELS, OPERATOR_LABELS, PERIOD_LABELS, MonitorService
from .notifier import NotificationTarget, Notifier
from .quote_provider import build_quote_provider
from .repository import MonitorRepository
from .schemas import (
    AlertPageOut,
    AuthLoginRequest,
    AuthLoginResponse,
    AuthRegisterRequest,
    AuthStatus,
    ClearAlertsResponse,
    InstrumentType,
    MetadataOut,
    MonitorCreate,
    MonitorOut,
    MonitorUpdate,
    NotificationSettings,
    NotificationSettingsOut,
    PasswordChangeRequest,
    PollIntervalUpdate,
    RuleCreate,
    RuleOut,
    RuleUpdate,
    SystemInfo,
    TestNotificationRequest,
    UserAuthRecord,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
settings = Settings.from_env()
settings.ensure_directories()
database = Database(settings.sqlite_path)
repository = MonitorRepository(database)
quote_provider = build_quote_provider(settings.quote_provider)
monitor_service = MonitorService(repository, quote_provider, settings.poll_interval_seconds)
auth_manager = LoginAuthManager()


def load_saved_poll_interval() -> None:
    saved_value = repository.get_app_setting("poll_interval_seconds")
    if not saved_value:
        return
    try:
        settings.poll_interval_seconds = max(int(saved_value), 60)
    except ValueError:
        return
    monitor_service.poll_interval_seconds = settings.poll_interval_seconds


def bootstrap_default_user() -> None:
    if repository.count_users() > 0:
        return
    if settings.login_username and settings.login_password:
        try:
            repository.create_user(settings.login_username, auth_manager.create_secret_hash(settings.login_password))
        except ValueError:
            pass


def auth_token_from_request(request: Request) -> str:
    return request.headers.get("X-Auth-Token", "").strip()


def require_current_user(request: Request) -> UserAuthRecord:
    token = auth_token_from_request(request)
    session = auth_manager.get_session(token)
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    user = repository.get_user_by_username(session.username)
    if user is None or not user.enabled:
        auth_manager.logout(token)
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def notification_out() -> NotificationSettingsOut:
    saved = repository.get_notification_settings()
    configured = (
        saved.channel == "wecom"
        and bool(saved.webhook_url)
        or saved.channel == "bark"
        and bool(saved.bark_url)
        or saved.channel == "webhook"
        and bool(saved.webhook_url)
    )
    return NotificationSettingsOut(
        channel=saved.channel,
        webhook_url=saved.webhook_url,
        bark_url=saved.bark_url,
        configured=configured,
    )


class MonitorPreflightRequest(BaseModel):
    code: str


class InstrumentSearchItem(BaseModel):
    code: str
    name: str
    instrument_type: InstrumentType


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    diagnostics = database.diagnostics()
    if not diagnostics.get("writable") or not diagnostics.get("schema_ok"):
        logger.error("Database startup check failed: %s", diagnostics)
    bootstrap_default_user()
    load_saved_poll_interval()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


@app.exception_handler(sqlite3.OperationalError)
async def handle_database_error(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    message = str(exc)
    logger.exception("Database error on %s %s", request.method, request.url.path)
    if "no column named" in message:
        code = "database_schema_outdated"
        detail = "数据库结构落后于程序版本，服务启动时自动升级未生效，请查看服务日志"
    elif "readonly" in message or "unable to open" in message:
        code = "database_not_writable"
        detail = "数据库文件不可写，请检查数据目录权限"
    elif "locked" in message or "busy" in message:
        code = "database_busy"
        detail = "数据库正忙，请稍后重试"
    else:
        code = "database_error"
        detail = "数据库操作失败，请稍后重试"
    return JSONResponse(
        status_code=500,
        content={
            "detail": detail,
            "code": code,
            "path": request.url.path,
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误，请稍后重试；若持续出现请联系管理员查看服务日志",
            "code": "internal_error",
            "path": request.url.path,
        },
    )


@app.middleware("http")
async def disable_stale_asset_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, object]:
    diagnostics = database.diagnostics()
    healthy = bool(diagnostics.get("writable")) and bool(diagnostics.get("schema_ok"))
    return {
        "status": "ok" if healthy else "degraded",
        "service": "stock-radar",
        "database": diagnostics,
    }


@app.get("/api/system", response_model=SystemInfo)
def get_system_info() -> SystemInfo:
    stats = repository.get_stats()
    notification = notification_out()
    data_status = repository.get_app_setting("data_status") or "unknown"
    data_message = repository.get_app_setting("data_message") or ""
    return SystemInfo(
        app_name=settings.app_name,
        poll_interval_seconds=monitor_service.poll_interval_seconds,
        quote_provider="东方财富 + 新浪 + 腾讯",
        market_open=market_open(),
        market_status=market_status_text(),
        last_cycle_at=repository.get_app_setting("last_cycle_at") or "",
        next_cycle_at=repository.get_app_setting("next_cycle_at") or "",
        monitor_count=stats["monitor_count"],
        alert_count=stats["alert_count"],
        notification_channel=notification.channel,
        notification_configured=notification.configured,
        data_status=data_status,
        data_message=data_message,
        data_updated_at=repository.get_app_setting("last_cycle_at") or "",
    )


@app.get("/api/auth/status", response_model=AuthStatus)
def get_auth_status(request: Request) -> AuthStatus:
    session = auth_manager.get_session(auth_token_from_request(request))
    count = repository.count_users()
    return AuthStatus(
        authenticated=session is not None,
        username=session.username if session else "",
        account_initialized=count > 0,
        registration_enabled=settings.allow_registration,
    )


@app.post("/api/auth/login", response_model=AuthLoginResponse)
def login(payload: AuthLoginRequest) -> AuthLoginResponse:
    if repository.count_users() == 0:
        raise HTTPException(status_code=400, detail="系统还没有账号，请先注册一个账号")
    user = repository.get_user_by_username(payload.username)
    if user is None or not user.enabled or not auth_manager.verify_secret_hash(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    session = auth_manager.create_session(user.username)
    return AuthLoginResponse(authenticated=True, username=session.username, token=session.token, expires_at=session.expires_at)


@app.post("/api/auth/register", response_model=AuthLoginResponse)
def register(payload: AuthRegisterRequest) -> AuthLoginResponse:
    if not settings.allow_registration:
        raise HTTPException(status_code=403, detail="注册功能已关闭")
    try:
        user = repository.create_user(payload.username, auth_manager.create_secret_hash(payload.password))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    repository.create_system_event("config_changed", {"kind": "user_registered", "username": user.username})
    session = auth_manager.create_session(user.username)
    return AuthLoginResponse(authenticated=True, username=session.username, token=session.token, expires_at=session.expires_at)


@app.post("/api/auth/change-password", response_model=AuthLoginResponse)
def change_password(payload: PasswordChangeRequest, request: Request) -> AuthLoginResponse:
    user = require_current_user(request)
    if not auth_manager.verify_secret_hash(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    updated_user = repository.update_user_password(user.username, auth_manager.create_secret_hash(payload.new_password))
    auth_manager.revoke_user_sessions(updated_user.username)
    repository.create_system_event("config_changed", {"kind": "user_password_changed", "username": updated_user.username})
    session = auth_manager.create_session(updated_user.username)
    return AuthLoginResponse(authenticated=True, username=session.username, token=session.token, expires_at=session.expires_at)


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request) -> Response:
    auth_manager.logout(auth_token_from_request(request))
    return Response(status_code=204)


@app.get("/api/events")
async def stream_events(request: Request) -> StreamingResponse:
    try:
        last_event_id = int(request.headers.get("Last-Event-ID", "0") or "0")
    except ValueError:
        last_event_id = 0

    async def event_stream():
        nonlocal last_event_id
        last_ping = asyncio.get_running_loop().time()
        yield "event: ready\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = await asyncio.to_thread(repository.list_system_events_after, last_event_id, 50)
            if events:
                for event in events:
                    last_event_id = event.id
                    yield f"id: {event.id}\nevent: {event.event_name}\ndata: {event.payload}\n\n"
                last_ping = asyncio.get_running_loop().time()
                continue
            if asyncio.get_running_loop().time() - last_ping >= 20:
                last_ping = asyncio.get_running_loop().time()
                yield ": ping\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/metadata", response_model=MetadataOut)
def get_metadata() -> MetadataOut:
    return MetadataOut(
        instrument_types=[
            {"value": InstrumentType.STOCK.value, "label": "股票"},
            {"value": InstrumentType.INDEX.value, "label": "指数"},
        ],
        fields=[{"value": value, "label": label} for value, label in FIELD_LABELS.items()],
        operators=[{"value": value, "label": label} for value, label in OPERATOR_LABELS.items()],
        periods=[{"value": value, "label": label} for value, label in PERIOD_LABELS.items()],
    )


@app.get("/api/instruments/search", response_model=list[InstrumentSearchItem])
async def search_instruments_get(keyword: str, request: Request) -> list[InstrumentSearchItem]:
    require_current_user(request)
    keyword = keyword.strip()
    if not keyword:
        return []
    search = getattr(quote_provider, "search_instruments", None)
    if search is None:
        return []
    try:
        results = await search(keyword, limit=8)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"股票名称搜索暂时不可用：{exc}") from exc
    return [InstrumentSearchItem(**item) for item in results]


@app.post("/api/monitors/preflight")
async def preflight_monitor(payload: MonitorPreflightRequest, request: Request) -> dict[str, str]:
    require_current_user(request)
    from .utils import normalize_code

    raw_code = payload.code.strip()
    instrument_type = InstrumentType.STOCK
    try:
        normalized = normalize_code(raw_code, "stock")
    except ValueError as exc:
        if any(ch.isalpha() for ch in raw_code):
            search = getattr(quote_provider, "search_instruments", None)
            if search is None:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                results = await search(raw_code, limit=5)
            except Exception:
                results = []
            if not results:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if len(results) > 1:
                names = "、".join(f"{item['name']}({item['code']})" for item in results[:5])
                raise HTTPException(status_code=400, detail=f"找到多个匹配，请从候选列表选择：{names}") from exc
            normalized = results[0]["code"]
            instrument_type = InstrumentType(results[0]["instrument_type"])
        else:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    from .schemas import QuoteTarget

    quotes = await quote_provider.fetch_many([QuoteTarget(code=normalized, instrument_type=instrument_type)])
    quote = next(iter(quotes.values()), None)
    if quote is None:
        raise HTTPException(status_code=400, detail=f"找不到 {normalized} 的行情，请检查代码")
    return {
        "code": normalized,
        "name": quote.name,
        "last_price": str(quote.last_price),
        "change_pct": str(quote.change_pct),
        "instrument_type": instrument_type.value,
    }


@app.post("/api/monitors/{monitor_id}/refresh")
async def refresh_single_monitor(monitor_id: int, request: Request) -> dict[str, int]:
    user = require_current_user(request)
    monitor = repository.get_monitor(monitor_id, user_id=user.id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="监控项不存在")
    quotes = await monitor_service._fetch_quotes([monitor], publish_event=False)
    return {"updated": len(quotes)}


@app.get("/api/monitors", response_model=list[MonitorOut])
async def list_monitors(request: Request) -> list[MonitorOut]:
    user = require_current_user(request)
    return await monitor_service.list_monitors_with_quotes(user_id=user.id)


@app.post("/api/quotes/refresh")
async def refresh_quotes(request: Request) -> dict[str, int]:
    user = require_current_user(request)
    return await monitor_service.refresh_quotes_for_all(user_id=user.id)


@app.put("/api/system/poll-interval", response_model=SystemInfo)
def update_poll_interval(payload: PollIntervalUpdate, request: Request) -> SystemInfo:
    require_current_user(request)
    repository.set_app_setting("poll_interval_seconds", str(payload.poll_interval_seconds))
    monitor_service.poll_interval_seconds = payload.poll_interval_seconds
    repository.create_system_event("system_updated", {"poll_interval_seconds": payload.poll_interval_seconds})
    return get_system_info()


@app.get("/api/settings/notification", response_model=NotificationSettingsOut)
def get_notification_settings(request: Request) -> NotificationSettingsOut:
    require_current_user(request)
    return notification_out()


@app.put("/api/settings/notification", response_model=NotificationSettingsOut)
def update_notification_settings(payload: NotificationSettings, request: Request) -> NotificationSettingsOut:
    require_current_user(request)
    repository.save_notification_settings(payload)
    repository.create_system_event("system_updated", {"kind": "notification_settings", "channel": payload.channel})
    return notification_out()


@app.post("/api/settings/notification/test")
async def test_notification(payload: TestNotificationRequest, request: Request) -> dict[str, str]:
    require_current_user(request)
    notifier = Notifier()
    await notifier.start()
    try:
        await notifier.send(
            NotificationTarget(channel=payload.channel, webhook_url=payload.webhook_url, bark_url=payload.bark_url),
            "股价雷达测试",
            "这是一条测试消息，说明通知渠道可用。",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"发送失败：{exc}") from exc
    finally:
        await notifier.stop()
    return {"status": "sent"}


@app.post("/api/monitors", response_model=MonitorOut, status_code=201)
def create_monitor(payload: MonitorCreate, request: Request) -> MonitorOut:
    user = require_current_user(request)
    try:
        monitor = repository.create_monitor(payload, user_id=user.id)
        repository.create_system_event("config_changed", {"kind": "monitor_created"})
        return monitor
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/monitors/{monitor_id}", response_model=MonitorOut)
def update_monitor(monitor_id: int, payload: MonitorUpdate, request: Request) -> MonitorOut:
    user = require_current_user(request)
    try:
        monitor = repository.update_monitor(monitor_id, payload, user_id=user.id)
        repository.create_system_event("config_changed", {"kind": "monitor_updated", "monitor_id": monitor_id})
        return monitor
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/monitors/{monitor_id}", status_code=204)
def delete_monitor(monitor_id: int, request: Request) -> Response:
    user = require_current_user(request)
    repository.delete_monitor(monitor_id, user_id=user.id)
    repository.create_system_event("config_changed", {"kind": "monitor_deleted", "monitor_id": monitor_id})
    return Response(status_code=204)


@app.post("/api/monitors/{monitor_id}/rules", response_model=RuleOut, status_code=201)
def create_rule(monitor_id: int, payload: RuleCreate, request: Request) -> RuleOut:
    user = require_current_user(request)
    try:
        rule = repository.create_rule(monitor_id, payload, user_id=user.id)
        repository.create_system_event("config_changed", {"kind": "rule_created", "monitor_id": monitor_id})
        return rule
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/rules/{rule_id}", response_model=RuleOut)
def update_rule(rule_id: int, payload: RuleUpdate, request: Request) -> RuleOut:
    user = require_current_user(request)
    try:
        rule = repository.update_rule(rule_id, payload, user_id=user.id)
        repository.create_system_event("config_changed", {"kind": "rule_updated", "rule_id": rule_id})
        return rule
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int, request: Request) -> Response:
    user = require_current_user(request)
    repository.delete_rule(rule_id, user_id=user.id)
    repository.create_system_event("config_changed", {"kind": "rule_deleted", "rule_id": rule_id})
    return Response(status_code=204)


@app.get("/api/alerts", response_model=AlertPageOut)
def list_alerts(request: Request, page: int = 1, page_size: int = 20) -> AlertPageOut:
    user = require_current_user(request)
    safe_page = max(page, 1)
    safe_page_size = min(max(page_size, 1), 100)
    items, total = repository.list_alerts(page=safe_page, page_size=safe_page_size, user_id=user.id)
    total_pages = max((total + safe_page_size - 1) // safe_page_size, 1)
    return AlertPageOut(items=items, page=safe_page, page_size=safe_page_size, total=total, total_pages=total_pages)


@app.delete("/api/alerts", response_model=ClearAlertsResponse)
def clear_alerts(request: Request) -> ClearAlertsResponse:
    user = require_current_user(request)
    cleared = repository.clear_alerts(user_id=user.id)
    repository.create_system_event("alerts_updated", {"kind": "alerts_cleared", "cleared": cleared})
    return ClearAlertsResponse(cleared=cleared)


@app.post("/api/run-once")
async def run_once(request: Request) -> dict[str, object]:
    user = require_current_user(request)
    result = await monitor_service.run_cycle(user_id=user.id, force=True)
    payload = {"kind": "run_once_completed", **asdict(result)}
    repository.create_system_event("system_updated", payload)
    return asdict(result)
