import asyncio
import sqlite3

from starlette.requests import Request

from backend.app.main import handle_database_error, handle_unexpected_error


def make_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
        }
    )


def test_database_schema_error_is_structured():
    response = asyncio.run(
        handle_database_error(
            make_request("/api/monitors/7/rules"),
            sqlite3.OperationalError("table rules has no column named ma_period"),
        )
    )

    assert response.status_code == 500
    payload = response.body.decode()
    assert "database_schema_outdated" in payload
    assert "/api/monitors/7/rules" in payload


def test_unhandled_error_is_structured():
    response = asyncio.run(
        handle_unexpected_error(
            make_request("/api/system"),
            RuntimeError("boom"),
        )
    )

    assert response.status_code == 500
    payload = response.body.decode()
    assert "internal_error" in payload
    assert "服务器内部错误" in payload
