import sqlite3

from backend.app.database import Database


def test_init_db_upgrades_legacy_rules_table(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            field TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold REAL NOT NULL,
            period TEXT NOT NULL DEFAULT 'daily',
            cooldown_minutes INTEGER NOT NULL DEFAULT 30,
            consecutive_hits_required INTEGER NOT NULL DEFAULT 1,
            current_consecutive_hits INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL DEFAULT '',
            last_triggered_at TEXT,
            last_trigger_value REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO rules (
            monitor_id, field, operator, threshold, period,
            created_at, updated_at
        ) VALUES (1, 'last_price', 'gte', 10, 'daily', '2026-11-14', '2026-11-14')
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.init_db()

    with database.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(rules)")}
        row = conn.execute("SELECT ma_period FROM rules WHERE id = 1").fetchone()
    assert "ma_period" in columns
    assert row["ma_period"] == 20


def test_diagnostics_reports_writable_and_schema_ok(tmp_path):
    database = Database(tmp_path / "ok.db")
    database.init_db()

    diagnostics = database.diagnostics()

    assert diagnostics["writable"] is True
    assert diagnostics["schema_ok"] is True
    assert diagnostics["missing_columns"] == []
    assert "error" not in diagnostics
