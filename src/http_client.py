"""HTTP client dùng khi recon.py đã tìm ra API JSON của Trip.com.

Đây là đường nhanh: không browser, không Playwright, gấp 5–10 lần crawl_list.py.
Đã có sẵn giới hạn tốc độ, retry với backoff, và ghi nhận lỗi thay vì nuốt lỗi.

    from http_client import TripClient

    async with TripClient() as c:
        data = await c.get_json(url, params={"pageIndex": 1})
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

import config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class RateLimiter:
    """Giãn cách ngẫu nhiên giữa các request, tự nới ra khi bị 429/403."""

    def __init__(self, lo: float, hi: float, ceiling: float = 30.0) -> None:
        self.lo, self.hi, self.ceiling = lo, hi, ceiling
        self.penalty = 0.0
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            gap = random.uniform(self.lo, self.hi) + self.penalty
            delta = time.monotonic() - self._last
            if delta < gap:
                await asyncio.sleep(gap - delta)
            self._last = time.monotonic()

    def backoff(self) -> None:
        self.penalty = min(self.ceiling, max(1.0, self.penalty * 2))

    def relax(self) -> None:
        self.penalty = max(0.0, self.penalty * 0.5)


class TripClient:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.limiter = RateLimiter(config.MIN_DELAY, config.MAX_DELAY)
        self.sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
        self.errors: list[dict[str, Any]] = []
        self._headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            "Referer": "https://vn.trip.com/hotels/",
            "Origin": "https://vn.trip.com",
            **(headers or {}),
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TripClient":
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            http2=True,
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    async def get_json(
        self, url: str, *, params: dict | None = None, json_body: dict | None = None,
        attempts: int = 4,
    ) -> Any | None:
        """Trả về JSON, hoặc None sau khi hết lượt thử. Lỗi ghi vào self.errors."""
        assert self._client, "dùng trong 'async with TripClient()'"
        method = "POST" if json_body is not None else "GET"

        for i in range(attempts):
            async with self.sem:
                await self.limiter.wait()
                try:
                    r = await self._client.request(method, url, params=params, json=json_body)
                except httpx.HTTPError as e:
                    self._note(url, None, f"{type(e).__name__}: {e}")
                    await asyncio.sleep(2 ** i)
                    continue

            if r.status_code in (429, 403, 503):
                self.limiter.backoff()
                self._note(url, r.status_code, "bị giới hạn tốc độ, đang giãn ra")
                await asyncio.sleep((2 ** i) + random.uniform(0, 2))
                continue

            if r.status_code >= 500:
                await asyncio.sleep(2 ** i)
                continue

            if r.status_code >= 400:
                self._note(url, r.status_code, r.text[:300])
                return None

            self.limiter.relax()
            try:
                return r.json()
            except ValueError:
                self._note(url, r.status_code, "response không phải JSON")
                return None

        self._note(url, None, f"bỏ cuộc sau {attempts} lần thử")
        return None

    def _note(self, url: str, status: int | None, msg: str) -> None:
        self.errors.append({"url": url, "status": status, "error": msg})
        print(f"  ! {status or '---'} {url.split('?')[0]} — {msg}")
