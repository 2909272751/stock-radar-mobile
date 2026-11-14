from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import json
import sqlite3

from .database import Database
from .schemas import (
    AlertEventOut,
    IndicatorOut,
    MonitorCreate,
    MonitorOut,
    MonitorUpdate,
    NotificationSettings,
    QuoteSnapshot,
    RuleCreate,
    RuleOut,
    RuleUpdate,
    SystemEventRecord,
    UserAuthRecord,
    WebhookJobRecord,
)
from .utils import infer_market, normalize_code, now_iso, now_local, quote_cache_key


class MonitorRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _row_to_rule(self, row: sqlite3.Row) -> RuleOut:
        return RuleOut(
            id=row["id"],
            monitor_id=row["monitor_id"],
            field=row["field"],
            operator=row["operator"],
            threshold=row["threshold"],
            period=row["period"],
            ma_period=row["ma_period"] if "ma_period" in row.keys() else 20,
            cooldown_minutes=row["cooldown_minutes"],
            consecutive_hits_required=row["consecutive_hits_required"],
            current_consecutive_hits=row["current_consecutive_hits"],
            enabled=bool(row["enabled"]),
            description=row["description"],
            last_triggered_at=row["last_triggered_at"],
            last_trigger_value=row["last_trigger_value"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _row_to_monitor(self, row: sqlite3.Row, rules: list[RuleOut] | None = None) -> MonitorOut:
        return MonitorOut(
            id=row["id"],
            instrument_type=row["instrument_type"],
            code=row["code"],
            market=row["market"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            rules=rules or [],
        )

    def _row_to_alert(self, row: sqlite3.Row) -> AlertEventOut:
        return AlertEventOut(
            id=row["id"],
            monitor_id=row["monitor_id"],
            rule_id=row["rule_id"],
            code=row["code"],
            name=row["name"],
            message=row["message"],
            status=row["status"],
            triggered_value=row["triggered_value"],
            rule_summary=row["rule_summary"],
            error=row["error"],
            created_at=row["created_at"],
        )

    def _row_to_quote(self, row: sqlite3.Row) -> QuoteSnapshot:
        return QuoteSnapshot(
            code=row["code"],
            instrument_type=row["instrument_type"],
            name=row["name"],
            last_price=row["last_price"],
            change_pct=row["change_pct"],
            change_amount=row["change_amount"],
            open_price=row["open_price"],
            high_price=row["high_price"],
            low_price=row["low_price"],
            prev_close=row["prev_close"],
            volume=row["volume"],
            turnover=row["turnover"],
            turnover_rate=row["turnover_rate"],
            pe_ttm=row["pe_ttm"],
            timestamp=row["quote_timestamp"],
            source=row["source"],
        )

    def _row_to_indicator(self, row: sqlite3.Row) -> IndicatorOut:
        return IndicatorOut(
            period=row["period"],
            dif=row["dif"],
            dea=row["dea"],
            histogram=row["histogram"],
            crossed_up=bool(row["crossed_up"]),
            crossed_down=bool(row["crossed_down"]),
            bars=row["bars"],
            updated_at=row["updated_at"],
        )

    def _row_to_system_event(self, row: sqlite3.Row) -> SystemEventRecord:
        return SystemEventRecord(
            id=row["id"],
            event_name=row["event_name"],
            payload=row["payload"],
            created_at=row["created_at"],
        )

    def _row_to_user(self, row: sqlite3.Row) -> UserAuthRecord:
        return UserAuthRecord(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def count_users(self) -> int:
        with self.db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"])

    def get_user_by_username(self, username: str) -> UserAuthRecord | None:
        clean_username = (username or "").strip()
        if not clean_username:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (clean_username,)).fetchone()
        return self._row_to_user(row) if row else None

    def create_user(self, username: str, password_hash: str) -> UserAuthRecord:
        clean_username = (username or "").strip()
        if not clean_username:
            raise ValueError("用户名不能为空")
        timestamp = now_iso()
        with self.db.connect() as conn:
            try:
                cursor = conn.execute(
                    "INSERT INTO users (username, password_hash, enabled, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (clean_username, password_hash, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"用户名 {clean_username} 已存在") from exc
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_user(row)

    def update_user_password(self, username: str, password_hash: str) -> UserAuthRecord:
        clean_username = (username or "").strip()
        timestamp = now_iso()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (clean_username,)).fetchone()
            if not row:
                raise ValueError("用户不存在")
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (password_hash, timestamp, clean_username),
            )
            updated_row = conn.execute("SELECT * FROM users WHERE username = ?", (clean_username,)).fetchone()
        return self._row_to_user(updated_row)

    def list_monitors(self, user_id: int | None = None) -> list[MonitorOut]:
        with self.db.connect() as conn:
            if user_id is None:
                monitor_rows = conn.execute(
                    "SELECT * FROM monitors ORDER BY enabled DESC, id ASC"
                ).fetchall()
                rule_rows = conn.execute("SELECT * FROM rules ORDER BY id ASC").fetchall()
            else:
                monitor_rows = conn.execute(
                    "SELECT * FROM monitors WHERE user_id = ? ORDER BY enabled DESC, id ASC",
                    (user_id,),
                ).fetchall()
                rule_rows = conn.execute(
                    """
                    SELECT rules.* FROM rules
                    INNER JOIN monitors ON monitors.id = rules.monitor_id
                    WHERE monitors.user_id = ?
                    ORDER BY rules.id ASC
                    """,
                    (user_id,),
                ).fetchall()

        rules_by_monitor: dict[int, list[RuleOut]] = defaultdict(list)
        for row in rule_rows:
            rules_by_monitor[row["monitor_id"]].append(self._row_to_rule(row))
        return [self._row_to_monitor(row, rules_by_monitor.get(row["id"], [])) for row in monitor_rows]

    def list_enabled_monitors(self, user_id: int | None = None) -> list[MonitorOut]:
        return [monitor for monitor in self.list_monitors(user_id=user_id) if monitor.enabled]

    def create_system_alert(self, user_id: int | None, title: str, message: str, code: str = "SYSTEM", name: str = "数据源告警") -> AlertEventOut:
        timestamp = now_iso()
        monitor_id = 0
        with self.db.connect() as conn:
            if user_id is None:
                row = conn.execute("SELECT id FROM monitors ORDER BY id ASC LIMIT 1").fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM monitors WHERE user_id = ? ORDER BY id ASC LIMIT 1",
                    (user_id,),
                ).fetchone()
            if row:
                monitor_id = int(row["id"])
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alert_events (
                    monitor_id, rule_id, code, name, message, status, triggered_value,
                    rule_summary, error, created_at
                )
                VALUES (?, NULL, ?, ?, ?, 'queued', NULL, '数据源告警', '', ?)
                """,
                (monitor_id, code, name, f"{title}\n{message}", timestamp),
            )
            alert_event_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO notification_jobs (
                    alert_event_id, monitor_id, rule_id, code, title, message, status,
                    attempt_count, max_attempts, available_at, locked_at, last_error, sent_at,
                    created_at, updated_at
                )
                VALUES (?, ?, NULL, ?, ?, ?, 'pending', 0, 3, ?, NULL, '', NULL, ?, ?)
                """,
                (alert_event_id, monitor_id, code, f"{name} ({code})", message, timestamp, timestamp, timestamp),
            )
            row = conn.execute("SELECT * FROM alert_events WHERE id = ?", (alert_event_id,)).fetchone()
        return self._row_to_alert(row)

    def get_monitor(self, monitor_id: int, user_id: int | None = None) -> MonitorOut | None:
        for monitor in self.list_monitors(user_id=user_id):
            if monitor.id == monitor_id:
                return monitor
        return None

    def create_monitor(self, payload: MonitorCreate, user_id: int) -> MonitorOut:
        timestamp = now_iso()
        code = normalize_code(payload.code, payload.instrument_type.value)
        market = infer_market(code[:6], payload.instrument_type.value)
        with self.db.connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO monitors (user_id, instrument_type, code, market, name, enabled, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        payload.instrument_type.value,
                        code,
                        market,
                        payload.name.strip(),
                        int(payload.enabled),
                        payload.note.strip(),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"监控对象 {code} 已存在，请直接编辑现有配置") from exc
            row = conn.execute("SELECT * FROM monitors WHERE id = ? AND user_id = ?", (cursor.lastrowid, user_id)).fetchone()
        return self._row_to_monitor(row, [])

    def update_monitor(self, monitor_id: int, payload: MonitorUpdate, user_id: int) -> MonitorOut:
        timestamp = now_iso()
        code = normalize_code(payload.code, payload.instrument_type.value)
        market = infer_market(code[:6], payload.instrument_type.value)
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)).fetchone()
            if not existing:
                raise ValueError("监控项不存在")
            duplicate = conn.execute(
                "SELECT id FROM monitors WHERE user_id = ? AND code = ? AND instrument_type = ? AND id != ?",
                (user_id, code, payload.instrument_type.value, monitor_id),
            ).fetchone()
            if duplicate:
                raise ValueError(f"监控对象 {code} 已被其他监控项占用")
            conn.execute(
                """
                UPDATE monitors
                SET instrument_type = ?, code = ?, market = ?, name = ?, enabled = ?, note = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    payload.instrument_type.value,
                    code,
                    market,
                    payload.name.strip(),
                    int(payload.enabled),
                    payload.note.strip(),
                    timestamp,
                    monitor_id,
                    user_id,
                ),
            )
            row = conn.execute("SELECT * FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)).fetchone()
            rule_rows = conn.execute("SELECT * FROM rules WHERE monitor_id = ? ORDER BY id ASC", (monitor_id,)).fetchall()
        return self._row_to_monitor(row, [self._row_to_rule(rule_row) for rule_row in rule_rows])

    def delete_monitor(self, monitor_id: int, user_id: int) -> None:
        with self.db.connect() as conn:
            rule_ids = [
                row["id"] for row in conn.execute("SELECT id FROM rules WHERE monitor_id = ?", (monitor_id,)).fetchall()
            ]
            conn.execute("DELETE FROM notification_jobs WHERE monitor_id = ?", (monitor_id,))
            conn.execute("DELETE FROM alert_events WHERE monitor_id = ?", (monitor_id,))
            conn.execute("DELETE FROM rules WHERE monitor_id = ?", (monitor_id,))
            conn.execute("DELETE FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id))
            if rule_ids:
                conn.execute("DELETE FROM indicator_snapshots WHERE cache_key IN (SELECT cache_key FROM quote_snapshots WHERE code = ?)", (monitor_id,))

    def create_rule(self, monitor_id: int, payload: RuleCreate, user_id: int) -> RuleOut:
        timestamp = now_iso()
        with self.db.connect() as conn:
            monitor = conn.execute("SELECT id FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)).fetchone()
            if not monitor:
                raise ValueError("监控项不存在，无法添加规则")
            duplicate = conn.execute(
                """
                SELECT rules.*
                FROM rules
                INNER JOIN monitors ON monitors.id = rules.monitor_id
                WHERE rules.monitor_id = ? AND monitors.user_id = ?
                  AND rules.field = ? AND rules.operator = ? AND rules.period = ?
                  AND rules.threshold = ? AND rules.ma_period = ?
                  AND rules.enabled = 1
                ORDER BY rules.id ASC
                LIMIT 1
                """,
                (
                    monitor_id,
                    user_id,
                    payload.field.value,
                    payload.operator.value,
                    payload.period,
                    payload.threshold,
                    payload.ma_period,
                ),
            ).fetchone()
            if duplicate:
                return self._row_to_rule(duplicate)
            cursor = conn.execute(
                """
                INSERT INTO rules (
                    monitor_id, field, operator, threshold, period, ma_period, cooldown_minutes,
                    consecutive_hits_required, current_consecutive_hits, enabled,
                    description, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    payload.field.value,
                    payload.operator.value,
                    payload.threshold,
                    payload.period,
                    payload.ma_period,
                    payload.cooldown_minutes,
                    payload.consecutive_hits_required,
                    int(payload.enabled),
                    payload.description.strip(),
                    timestamp,
                    timestamp,
                ),
            )
            row = conn.execute("SELECT * FROM rules WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_rule(row)

    def update_rule(self, rule_id: int, payload: RuleUpdate, user_id: int) -> RuleOut:
        timestamp = now_iso()
        with self.db.connect() as conn:
            existing = conn.execute(
                """
                SELECT rules.id
                FROM rules
                INNER JOIN monitors ON monitors.id = rules.monitor_id
                WHERE rules.id = ? AND monitors.user_id = ?
                """,
                (rule_id, user_id),
            ).fetchone()
            if not existing:
                raise ValueError("规则不存在")
            conn.execute(
                """
                UPDATE rules
                SET field = ?, operator = ?, threshold = ?, period = ?, ma_period = ?, cooldown_minutes = ?,
                    consecutive_hits_required = ?, current_consecutive_hits = 0,
                    enabled = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.field.value,
                    payload.operator.value,
                    payload.threshold,
                    payload.period,
                    payload.ma_period,
                    payload.cooldown_minutes,
                    payload.consecutive_hits_required,
                    int(payload.enabled),
                    payload.description.strip(),
                    timestamp,
                    rule_id,
                ),
            )
            row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return self._row_to_rule(row)

    def set_rule_hits(self, rule_id: int, consecutive_hits: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE rules SET current_consecutive_hits = ? WHERE id = ?",
                (max(consecutive_hits, 0), rule_id),
            )

    def claim_rule_trigger(self, rule_id: int, expected_hits: int, consumed_hits: int) -> bool:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE rules
                SET current_consecutive_hits = ?
                WHERE id = ? AND current_consecutive_hits = ?
                """,
                (max(consumed_hits, 0), rule_id, max(expected_hits, 0)),
            )
            return cursor.rowcount == 1

    def mark_rule_triggered(self, rule_id: int, triggered_value: float, timestamp: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE rules SET last_triggered_at = ?, last_trigger_value = ? WHERE id = ?",
                (timestamp, triggered_value, rule_id),
            )

    def delete_rule(self, rule_id: int, user_id: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """
                DELETE FROM rules
                WHERE id IN (
                    SELECT rules.id
                    FROM rules
                    INNER JOIN monitors ON monitors.id = rules.monitor_id
                    WHERE rules.id = ? AND monitors.user_id = ?
                )
                """,
                (rule_id, user_id),
            )

    def get_quote_map(self) -> dict[str, QuoteSnapshot]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM quote_snapshots").fetchall()
        return {quote_cache_key(row["code"], row["instrument_type"]): self._row_to_quote(row) for row in rows}

    def upsert_quotes(self, quotes: dict[str, QuoteSnapshot]) -> int:
        if not quotes:
            return 0
        timestamp = now_iso()
        rows = [
            (
                key,
                quote.code,
                quote.instrument_type.value,
                quote.name,
                quote.last_price,
                quote.change_pct,
                quote.change_amount,
                quote.open_price,
                quote.high_price,
                quote.low_price,
                quote.prev_close,
                quote.volume,
                quote.turnover,
                quote.turnover_rate,
                quote.pe_ttm,
                quote.timestamp,
                quote.source,
                timestamp,
            )
            for key, quote in quotes.items()
        ]
        with self.db.connect() as conn:
            conn.executemany(
                """
                INSERT INTO quote_snapshots (
                    cache_key, code, instrument_type, name, last_price, change_pct, change_amount,
                    open_price, high_price, low_price, prev_close, volume, turnover, turnover_rate,
                    pe_ttm, quote_timestamp, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    code = excluded.code,
                    instrument_type = excluded.instrument_type,
                    name = excluded.name,
                    last_price = excluded.last_price,
                    change_pct = excluded.change_pct,
                    change_amount = excluded.change_amount,
                    open_price = excluded.open_price,
                    high_price = excluded.high_price,
                    low_price = excluded.low_price,
                    prev_close = excluded.prev_close,
                    volume = excluded.volume,
                    turnover = excluded.turnover,
                    turnover_rate = excluded.turnover_rate,
                    pe_ttm = excluded.pe_ttm,
                    quote_timestamp = excluded.quote_timestamp,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_indicator_map(self) -> dict[tuple[str, str], IndicatorOut]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM indicator_snapshots").fetchall()
        return {(row["cache_key"], row["period"]): self._row_to_indicator(row) for row in rows}

    def upsert_indicators(self, cache_key: str, period: str, state: object) -> None:
        macd = getattr(state, "macd", None)
        timestamp = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO indicator_snapshots (
                    cache_key, period, dif, dea, histogram, prev_histogram,
                    crossed_up, crossed_down, bars, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key, period) DO UPDATE SET
                    dif = excluded.dif,
                    dea = excluded.dea,
                    histogram = excluded.histogram,
                    prev_histogram = excluded.prev_histogram,
                    crossed_up = excluded.crossed_up,
                    crossed_down = excluded.crossed_down,
                    bars = excluded.bars,
                    updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    period,
                    macd.dif if macd else None,
                    macd.dea if macd else None,
                    macd.histogram if macd else None,
                    macd.prev_histogram if macd else None,
                    int(macd.crossed_up) if macd else 0,
                    int(macd.crossed_down) if macd else 0,
                    getattr(state, "bars", 0),
                    timestamp,
                ),
            )

    def queue_alert(
        self,
        monitor_id: int,
        rule_id: int | None,
        code: str,
        name: str,
        title: str,
        message: str,
        triggered_value: float | None,
        rule_summary: str,
    ) -> AlertEventOut:
        timestamp = now_iso()
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alert_events (
                    monitor_id, rule_id, code, name, message, status, triggered_value,
                    rule_summary, error, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '', ?)
                """,
                (monitor_id, rule_id, code, name, message, triggered_value, rule_summary, timestamp),
            )
            alert_event_id = int(cursor.lastrowid)
            conn.execute(
                """
                INSERT INTO notification_jobs (
                    alert_event_id, monitor_id, rule_id, code, title, message, status,
                    attempt_count, max_attempts, available_at, locked_at, last_error, sent_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 3, ?, NULL, '', NULL, ?, ?)
                """,
                (alert_event_id, monitor_id, rule_id, code, f"{name} ({code})", message, timestamp, timestamp, timestamp),
            )
            row = conn.execute("SELECT * FROM alert_events WHERE id = ?", (alert_event_id,)).fetchone()
        return self._row_to_alert(row)

    def update_alert_status(self, alert_event_id: int, status: str, error: str = "") -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE alert_events SET status = ?, error = ? WHERE id = ?",
                (status, error, alert_event_id),
            )
            self._trim_alert_history(conn)

    def claim_notification_jobs(self, limit: int = 10, stale_after_minutes: int = 5) -> list[WebhookJobRecord]:
        timestamp = now_iso()
        stale_before = (now_local() - timedelta(minutes=max(stale_after_minutes, 1))).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM notification_jobs
                WHERE
                    ((status = 'pending' OR status = 'retrying') AND available_at <= ?)
                    OR (status = 'processing' AND locked_at IS NOT NULL AND locked_at <= ?)
                ORDER BY available_at ASC, id ASC
                LIMIT ?
                """,
                (timestamp, stale_before, max(limit, 1)),
            ).fetchall()
            if not rows:
                return []
            conn.executemany(
                "UPDATE notification_jobs SET status = 'processing', locked_at = ?, updated_at = ? WHERE id = ?",
                [(timestamp, timestamp, row["id"]) for row in rows],
            )
            refreshed = conn.execute(
                f"SELECT * FROM notification_jobs WHERE id IN ({','.join('?' for _ in rows)}) ORDER BY id ASC",
                [row["id"] for row in rows],
            ).fetchall()
        return [self._row_to_job(row) for row in refreshed]

    def _row_to_job(self, row: sqlite3.Row) -> WebhookJobRecord:
        return WebhookJobRecord(
            id=row["id"],
            alert_event_id=row["alert_event_id"],
            monitor_id=row["monitor_id"],
            rule_id=row["rule_id"],
            code=row["code"],
            webhook_url="",
            message=row["message"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            available_at=row["available_at"],
            locked_at=row["locked_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_job_title(self, job_id: int) -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT title FROM notification_jobs WHERE id = ?", (job_id,)).fetchone()
        return str(row["title"]) if row else ""

    def mark_job_sent(self, job_id: int, alert_event_id: int, attempt_count: int) -> None:
        timestamp = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE notification_jobs
                SET status = 'sent', attempt_count = ?, locked_at = NULL, last_error = '',
                    sent_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_count, timestamp, timestamp, job_id),
            )
        self.update_alert_status(alert_event_id, "sent", "")

    def mark_job_retry(self, job_id: int, alert_event_id: int, attempt_count: int, error: str, delay_seconds: int) -> None:
        timestamp = now_iso()
        available_at = (now_local() + timedelta(seconds=max(delay_seconds, 1))).isoformat(timespec="seconds")
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE notification_jobs
                SET status = 'retrying', attempt_count = ?, available_at = ?, locked_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_count, available_at, error, timestamp, job_id),
            )
        self.update_alert_status(alert_event_id, "retrying", error)

    def mark_job_failed(self, job_id: int, alert_event_id: int, attempt_count: int, error: str) -> None:
        timestamp = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE notification_jobs
                SET status = 'failed', attempt_count = ?, locked_at = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (attempt_count, error, timestamp, job_id),
            )
        self.update_alert_status(alert_event_id, "failed", error)

    def list_alerts(self, page: int = 1, page_size: int = 20, user_id: int | None = None) -> tuple[list[AlertEventOut], int]:
        offset = (max(page, 1) - 1) * max(page_size, 1)
        with self.db.connect() as conn:
            where = ""
            params: list[object] = []
            if user_id is not None:
                where = "WHERE monitors.user_id = ?"
                params.append(user_id)
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS total FROM alert_events
                    INNER JOIN monitors ON monitors.id = alert_events.monitor_id
                    {where}
                    """,
                    params,
                ).fetchone()["total"]
            )
            rows = conn.execute(
                f"""
                SELECT alert_events.* FROM alert_events
                INNER JOIN monitors ON monitors.id = alert_events.monitor_id
                {where}
                ORDER BY alert_events.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, max(page_size, 1), offset],
            ).fetchall()
        return [self._row_to_alert(row) for row in rows], total

    def clear_alerts(self, user_id: int) -> int:
        with self.db.connect() as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT alert_events.id FROM alert_events
                    INNER JOIN monitors ON monitors.id = alert_events.monitor_id
                    WHERE monitors.user_id = ? AND alert_events.status IN ('sent', 'failed')
                    """,
                    (user_id,),
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM notification_jobs WHERE alert_event_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM alert_events WHERE id IN ({placeholders})", ids)
        return len(ids)

    def _trim_alert_history(self, conn: sqlite3.Connection, limit: int = 500) -> None:
        conn.execute(
            """
            DELETE FROM alert_events
            WHERE id IN (
                SELECT id FROM alert_events
                ORDER BY created_at DESC, id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (max(limit, 1),),
        )

    def get_app_setting(self, key: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_app_setting(self, key: str, value: str) -> None:
        timestamp = now_iso()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, timestamp),
            )

    def get_notification_settings(self) -> NotificationSettings:
        return NotificationSettings(
            channel=self.get_app_setting("notification_channel") or "none",
            webhook_url=self.get_app_setting("notification_webhook_url") or "",
            bark_url=self.get_app_setting("notification_bark_url") or "",
        )

    def save_notification_settings(self, settings: NotificationSettings) -> None:
        self.set_app_setting("notification_channel", settings.channel)
        self.set_app_setting("notification_webhook_url", settings.webhook_url.strip())
        self.set_app_setting("notification_bark_url", settings.bark_url.strip())

    def create_system_event(self, event_name: str, payload: dict) -> int:
        timestamp = now_iso()
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO system_events (event_name, payload, created_at) VALUES (?, ?, ?)",
                (event_name, json.dumps(payload, ensure_ascii=False), timestamp),
            )
            conn.execute(
                """
                DELETE FROM system_events
                WHERE id NOT IN (SELECT id FROM system_events ORDER BY id DESC LIMIT 500)
                """
            )
        return int(cursor.lastrowid)

    def list_system_events_after(self, last_event_id: int, limit: int = 50) -> list[SystemEventRecord]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM system_events WHERE id > ? ORDER BY id ASC LIMIT ?",
                (max(last_event_id, 0), max(limit, 1)),
            ).fetchall()
        return [self._row_to_system_event(row) for row in rows]

    def get_stats(self) -> dict[str, int]:
        with self.db.connect() as conn:
            monitor_count = int(conn.execute("SELECT COUNT(*) AS total FROM monitors").fetchone()["total"])
            alert_count = int(conn.execute("SELECT COUNT(*) AS total FROM alert_events").fetchone()["total"])
            pending_jobs = int(
                conn.execute(
                    "SELECT COUNT(*) AS total FROM notification_jobs WHERE status IN ('pending', 'retrying', 'processing')"
                ).fetchone()["total"]
            )
        return {"monitor_count": monitor_count, "alert_count": alert_count, "pending_jobs": pending_jobs}
