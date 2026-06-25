"""Fetch and rotate free HTTP proxies from ProxyScrape on rate limits."""

from __future__ import annotations

import time
from typing import Any

import httpx

PROXYSCRAPE_API = "https://api.proxyscrape.com/v4/free-proxy-list/get"


class ProxyPool:
    """ProxyScrape free proxy list — https://proxyscrape.com/free-proxy-list"""

    def __init__(self, *, fetch_limit: int = 30, refresh_s: float = 300.0) -> None:
        self._fetch_limit = max(5, int(fetch_limit))
        self._refresh_s = max(60.0, float(refresh_s))
        self._proxies: list[str] = []
        self._index = 0
        self._failed: set[str] = set()
        self._fetched_at = 0.0

    @staticmethod
    def _label(proxy_url: str) -> str:
        return proxy_url.split("://", 1)[-1]

    def fetch(self) -> int:
        params: dict[str, Any] = {
            "request": "display_proxies",
            "proxy_format": "protocolipport",
            "format": "json",
            "protocol": "http,https",
            "limit": self._fetch_limit,
        }
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(PROXYSCRAPE_API, params=params)
            resp.raise_for_status()
            data = resp.json()

        proxies: list[str] = []
        ranked: list[tuple[float, str]] = []
        for entry in data.get("proxies", []):
            if not isinstance(entry, dict):
                continue
            url = entry.get("proxy")
            if not isinstance(url, str) or not url:
                continue
            if url in self._failed:
                continue
            if entry.get("alive") is False:
                continue
            latency = float(entry.get("timeout") or 99999)
            anon = str(entry.get("anonymity") or "")
            if anon == "transparent":
                latency += 5000
            elif anon == "anonymous":
                latency += 500
            ranked.append((latency, url))

        ranked.sort(key=lambda item: item[0])
        self._proxies = [url for _, url in ranked]
        self._index = 0
        self._fetched_at = time.time()
        return len(self._proxies)

    def next(self) -> str | None:
        stale = (time.time() - self._fetched_at) > self._refresh_s
        if not self._proxies or self._index >= len(self._proxies) or stale:
            self.fetch()

        while self._index < len(self._proxies):
            proxy = self._proxies[self._index]
            self._index += 1
            if proxy not in self._failed:
                return proxy
        return None

    def mark_failed(self, proxy_url: str | None) -> None:
        if proxy_url:
            self._failed.add(proxy_url)
