from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    instrument_type TEXT NOT NULL DEFAULT 'stock',
    code TEXT NOT NULL,
    market TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, instrument_type, code)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    operator TEXT NOT NULL,
    threshold REAL NOT NULL,
    period TEXT NOT NULL DEFAULT 'daily',
    ma_period INTEGER NOT NULL DEFAULT 20,
    cooldown_minutes INTEGER NOT NULL DEFAULT 30,
    consecutive_hits_required INTEGER NOT NULL DEFAULT 1,
    current_consecutive_hits INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    last_triggered_at TEXT,
    last_trigger_value REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL,
    rule_id INTEGER,
    code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    triggered_value REAL,
    rule_summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_snapshots (
    cache_key TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    last_price REAL NOT NULL DEFAULT 0,
    change_pct REAL NOT NULL DEFAULT 0,
    change_amount REAL NOT NULL DEFAULT 0,
    open_price REAL NOT NULL DEFAULT 0,
    high_price REAL NOT NULL DEFAULT 0,
    low_price REAL NOT NULL DEFAULT 0,
    prev_close REAL NOT NULL DEFAULT 0,
    volume REAL NOT NULL DEFAULT 0,
    turnover REAL NOT NULL DEFAULT 0,
    turnover_rate REAL NOT NULL DEFAULT 0,
    pe_ttm REAL NOT NULL DEFAULT 0,
    quote_timestamp TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS indicator_snapshots (
    cache_key TEXT NOT NULL,
    period TEXT NOT NULL,
    dif REAL,
    dea REAL,
    histogram REAL,
    prev_histogram REAL,
    crossed_up INTEGER NOT NULL DEFAULT 0,
    crossed_down INTEGER NOT NULL DEFAULT 0,
    bars INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cache_key, period)
);

CREATE TABLE IF NOT EXISTS notification_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_event_id INTEGER NOT NULL,
    monitor_id INTEGER NOT NULL,
    rule_id INTEGER,
    code TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rules_monitor_id ON rules (monitor_id);
CREATE INDEX IF NOT EXISTS idx_alert_events_created_at ON alert_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_events_monitor_id ON alert_events (monitor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_quote_snapshots_updated_at ON quote_snapshots (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_jobs_status ON notification_jobs (status, available_at, id);
CREATE INDEX IF NOT EXISTS idx_system_events_id ON system_events (id);
"""

# Columns added after the first release. Keep this list in sync with SCHEMA so
# databases created by older images are upgraded on startup instead of failing
# later on INSERT/UPDATE with "table ... has no column named ...".
RULE_COLUMN_MIGRATIONS: dict[str, str] = {
    "ma_period": "INTEGER NOT NULL DEFAULT 20",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
            self._migrate_columns(conn)
            indicator_columns = {
                row["name"]: row["pk"]
                for row in conn.execute("PRAGMA table_info(indicator_snapshots)").fetchall()
            }
            if indicator_columns and indicator_columns.get("period", 0) == 0:
                self._migrate_indicator_snapshots(conn)

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(rules)").fetchall()
        }
        for name, definition in RULE_COLUMN_MIGRATIONS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE rules ADD COLUMN {name} {definition}")

    @staticmethod
    def _migrate_indicator_snapshots(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE indicator_snapshots RENAME TO indicator_snapshots_legacy")
        conn.execute(
            """
            CREATE TABLE indicator_snapshots (
                cache_key TEXT NOT NULL,
                period TEXT NOT NULL,
                dif REAL,
                dea REAL,
                histogram REAL,
                prev_histogram REAL,
                crossed_up INTEGER NOT NULL DEFAULT 0,
                crossed_down INTEGER NOT NULL DEFAULT 0,
                bars INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (cache_key, period)
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO indicator_snapshots (
                cache_key, period, dif, dea, histogram, prev_histogram,
                crossed_up, crossed_down, bars, updated_at
            )
            SELECT cache_key, period, dif, dea, histogram, prev_histogram,
                   crossed_up, crossed_down, bars, updated_at
            FROM indicator_snapshots_legacy
            """
        )
        conn.execute("DROP TABLE indicator_snapshots_legacy")

    def diagnostics(self) -> dict[str, object]:
        """Return a serializable snapshot used by /api/health."""
        result: dict[str, object] = {
            "path": str(self.path),
            "exists": self.path.exists(),
            "writable": False,
            "schema_ok": False,
            "missing_columns": [],
        }
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(rules)").fetchall()
                }
                missing = sorted(set(RULE_COLUMN_MIGRATIONS) - columns)
                result["writable"] = True
                result["schema_ok"] = not missing
                result["missing_columns"] = missing
                conn.rollback()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        return result
