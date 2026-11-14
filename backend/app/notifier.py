from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import quote

import httpx


@dataclass(slots=True)
class NotificationTarget:
    channel: str
    webhook_url: str = ""
    bark_url: str = ""


class Notifier:
    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    trust_env=False,
                    follow_redirects=True,
                )

    async def stop(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            await self.start()
        assert self._client is not None
        return self._client

    async def send(self, target: NotificationTarget, title: str, content: str) -> None:
        channel = (target.channel or "none").strip().lower()
        if channel == "none":
            raise ValueError("尚未配置通知渠道，告警仅会保存在面板中")

        client = await self._get_client()
        if channel == "wecom":
            if not target.webhook_url:
                raise ValueError("企业微信 Webhook 为空")
            response = await client.post(
                target.webhook_url,
                json={"msgtype": "text", "text": {"content": f"{title}\n{content}"}},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errcode", 0) != 0:
                raise RuntimeError(f"企业微信返回错误：{payload}")
            return

        if channel == "bark":
            if not target.bark_url:
                raise ValueError("Bark 地址为空")
            base = target.bark_url.rstrip("/")
            url = f"{base}/{quote(title, safe='')}/{quote(content, safe='')}"
            response = await client.get(url, params={"group": "A股监控", "level": "timeSensitive"})
            response.raise_for_status()
            return

        if channel == "webhook":
            if not target.webhook_url:
                raise ValueError("Webhook 地址为空")
            response = await client.post(
                target.webhook_url,
                json={"title": title, "content": content, "text": f"{title}\n{content}", "source": "A股价格与指标提醒"},
            )
            response.raise_for_status()
            return

        raise ValueError(f"不支持的通知渠道：{channel}")
