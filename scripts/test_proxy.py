"""Kiểm tra nhanh proxy trong .env (không đụng browser_profile).

    python scripts\test_proxy.py

Thử 2 đường để khoanh vùng lỗi:
  1. httpx (không qua Chromium)  -> lỗi ở đây = do tài khoản/gói proxy
  2. Chromium Playwright         -> chỉ lỗi ở đây = do cách Chromium xác thực
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import config  # noqa: E402

URLS = ("https://api.ipify.org", "https://vn.trip.com/hotels/")


def test_httpx(proxy: dict) -> None:
    print("[1] httpx:")
    host = proxy["server"].split("://", 1)[1]
    url = (f"http://{proxy['username']}:{proxy['password']}@{host}"
           if proxy.get("username") else proxy["server"])
    try:
        with httpx.Client(proxy=url, timeout=30, follow_redirects=True) as c:
            for u in URLS:
                r = c.get(u)
                print(f"  {u} -> HTTP {r.status_code} {r.text[:40].strip() if 'ipify' in u else ''}")
    except Exception as exc:
        print(f"  LỖI {type(exc).__name__}: {str(exc)[:200]}")


async def test_chromium(proxy: dict) -> None:
    print("[2] Chromium:")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, proxy=proxy)
        page = await browser.new_page()
        for u in URLS:
            try:
                resp = await page.goto(u, wait_until="domcontentloaded", timeout=30000)
                body = (await page.inner_text("body"))[:40].strip() if "ipify" in u else ""
                print(f"  {u} -> HTTP {resp.status if resp else '?'} {body}")
            except Exception as exc:
                print(f"  {u} -> LỖI {str(exc).splitlines()[0]}")
        await browser.close()


if __name__ == "__main__":
    proxy = config.browser_proxy()
    if not proxy:
        raise SystemExit("Proxy đang TẮT: đặt TRIP_PROXY_ENABLED=true trong .env")
    print(f"Proxy: {proxy['server']}  user={proxy.get('username')}  "
          f"pass_len={len(proxy.get('password', ''))}\n")
    test_httpx(proxy)
    asyncio.run(test_chromium(proxy))
