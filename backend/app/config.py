from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


@dataclass(slots=True)
class Settings:
    app_name: str = "股价雷达"
    sqlite_path: Path = DATA_DIR / "monitor.db"
    poll_interval_seconds: int = 900
    quote_provider: str = "eastmoney"
    login_username: str = ""
    login_password: str = ""
    allow_registration: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        sqlite_path = Path(os.getenv("SQLITE_PATH", str(DATA_DIR / "monitor.db")))
        if not sqlite_path.is_absolute():
            sqlite_path = BASE_DIR / sqlite_path
        return cls(
            app_name=os.getenv("APP_NAME", "股价雷达").strip() or "股价雷达",
            sqlite_path=sqlite_path,
            poll_interval_seconds=max(int(os.getenv("POLL_INTERVAL_SECONDS", "900")), 60),
            quote_provider=os.getenv("QUOTE_PROVIDER", "eastmoney").strip().lower() or "eastmoney",
            login_username=os.getenv("LOGIN_USERNAME", "").strip(),
            login_password=os.getenv("LOGIN_PASSWORD", "").strip(),
            allow_registration=os.getenv("ALLOW_REGISTRATION", "1").strip().lower() in {"1", "true", "yes", "on"},
        )

    def ensure_directories(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
