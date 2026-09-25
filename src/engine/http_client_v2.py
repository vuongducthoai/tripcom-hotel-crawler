"""High-performance HTTP client powered by curl_cffi with Chrome TLS impersonation.

Features:
- Exact Chrome 124 TLS fingerprint (JA3/JA4, ALPN, cipher suites, HTTP/2 frames).
- Proxy session rotation per retry attempt to bypass WAF rate limits.
- Subcritical rate-limiting when running on direct IP.
- Semantic content validation before accepting responses.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from curl_cffi.requests import AsyncSession, Response

from engine.content_validator import validate_hotel_html
from engine.proxy_session import resolve_proxy_url

CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class RateLimiter:
    """Jittered rate limiter ensuring direct IP stays in the Subcritical Zone."""

    def __init__(self, lo: float = 1.5, hi: float = 3.5, ceiling: float = 30.0) -> None:
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
        self.penalty = min(self.ceiling, max(1.5, (self.penalty or 1.0) * 2))

    def relax(self) -> None:
        self.penalty = max(0.0, self.penalty * 0.5)


class FastHttpClient:
    """Async HTTP client supporting Chrome TLS impersonation and proxy rotation."""

    def __init__(
        self,
        base_proxy: str | None = None,
        min_delay: float = 1.5,
        max_delay: float = 3.5,
        impersonate: str = "chrome124",
    ) -> None:
        self.base_proxy = base_proxy
        self.has_proxy = bool(base_proxy)
        self.impersonate = impersonate
        # Rate limiter is strictly used when direct IP to stay subcritical
        self.limiter = RateLimiter(min_delay, max_delay)
        self.errors: list[dict[str, Any]] = []

    async def get_hotel_page(
        self,
        url: str,
        hotel_id: str | int | None = None,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        max_attempts: int = 5,
        timeout: float = 30.0,
    ) -> tuple[str | None, str | None]:
        """Fetch a hotel page with Chrome TLS fingerprint and retry on failure.

        Returns:
            (html_content, error_message)
        """
        merged_headers = {**CHROME_HEADERS, **(headers or {})}

        for attempt in range(1, max_attempts + 1):
            if not self.has_proxy:
                await self.limiter.wait()

            proxy_url = resolve_proxy_url(self.base_proxy, hotel_id, attempt)

            try:
                async with AsyncSession(
                    impersonate=self.impersonate,
                    proxy=proxy_url,
                    timeout=timeout,
                ) as session:
                    resp: Response = await session.get(
                        url,
                        headers=merged_headers,
                        cookies=cookies,
                        allow_redirects=True,
                    )
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
                self._record_error(url, None, err, attempt)
                if not self.has_proxy:
                    self.limiter.backoff()
                await asyncio.sleep(min(10.0, (1.5 ** attempt) + random.uniform(0.2, 1.0)))
                continue

            # Check status code
            if resp.status_code in (429, 432, 403, 503):
                reason = f"HTTP {resp.status_code} WAF / Rate-limit"
                self._record_error(url, resp.status_code, reason, attempt)
                if not self.has_proxy:
                    self.limiter.backoff()
                await asyncio.sleep(min(12.0, (2.0 ** attempt) + random.uniform(0.5, 1.5)))
                continue

            if resp.status_code >= 400:
                reason = f"HTTP {resp.status_code}"
                self._record_error(url, resp.status_code, reason, attempt)
                return None, reason

            # Semantic content validation: Verify it's not a block masked as 200 OK
            html_text = resp.text
            is_valid, validation_error = validate_hotel_html(html_text, expected_hotel_id=hotel_id)

            if not is_valid:
                self._record_error(url, resp.status_code, validation_error or "Validation failed", attempt)
                if not self.has_proxy:
                    self.limiter.backoff()
                await asyncio.sleep(min(8.0, (1.5 ** attempt) + random.uniform(0.2, 0.8)))
                continue

            # Successful valid hotel page!
            if not self.has_proxy:
                self.limiter.relax()
            return html_text, None

        final_err = f"Exceeded max retry attempts ({max_attempts})"
        self._record_error(url, None, final_err, max_attempts)
        return None, final_err

    async def get_nearby_places(
        self,
        hotel_id: str | int,
        city_id: int | None = None,
        province_id: int | None = None,
        lat: float | None = None,
        lng: float | None = None,
        locale: str = "vi-VN",
        currency: str = "VND",
        referer: str = "",
        timeout: float = 15.0,
    ) -> tuple[dict | None, str | None]:
        """Fetch full categorized nearby places via SOA2 ctGetNearbyPlaceInfo endpoint.
        
        Returns:
            (response_dict, error_message)
        """
        host = "www.trip.com" if locale.lower().startswith("en") else "vn.trip.com"
        url = f"https://{host}/restapi/soa2/28820/ctGetNearbyPlaceInfo"

        vid = f"{int(time.time()*1000)}.{random.randint(100000, 999999)}"
        post_headers = {
            "User-Agent": CHROME_HEADERS["User-Agent"],
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": f"https://{host}",
            "Referer": referer or f"https://{host}/hotels/detail/?hotelId={hotel_id}",
            "cookieorigin": f"https://{host}",
            "locale": locale,
            "currency": currency.upper(),
            "x-ctx-locale": locale,
            "x-ctx-currency": currency.upper(),
            "x-ctx-ubt-vid": vid,
            "Sec-Ch-Ua": CHROME_HEADERS["Sec-Ch-Ua"],
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        payload: dict[str, Any] = {
            "cityId": int(city_id or 0),
            "mapType": "gg",
            "masterHotelId": int(hotel_id),
            "oversea": False,
            "provinceId": int(province_id or 0),
            "head": {
                "platform": "PC",
                "cver": "0",
                "cid": vid,
                "bu": "IBU",
                "group": "trip",
                "locale": locale,
                "currency": currency.upper(),
                "timezone": "7",
                "pageId": "10320668147",
                "vid": vid,
                "isSSR": False,
            },
        }

        proxy_url = resolve_proxy_url(self.base_proxy, hotel_id, 1)
        try:
            async with AsyncSession(
                impersonate=self.impersonate,
                proxy=proxy_url,
                timeout=timeout,
            ) as session:
                resp = await session.post(url, json=payload, headers=post_headers)
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict) and data.get("data", {}).get("placeInfoList"):
                            return data, None
                        return None, "Empty placeInfoList"
                    except Exception as exc:
                        return None, f"JSON parse error: {exc}"
                return None, f"HTTP {resp.status_code} ({resp.text[:100]})"
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def _record_error(self, url: str, status: int | None, message: str, attempt: int) -> None:
        self.errors.append({
            "url": url,
            "status": status,
            "error": message,
            "attempt": attempt,
            "timestamp": time.time(),
        })

