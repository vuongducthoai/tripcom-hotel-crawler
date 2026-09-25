"""Tạo Chromium profile theo ngôn ngữ/tiền tệ đang crawl.

Chạy MỘT LẦN. Script mở một cửa sổ Chromium thật, anh tự tay:
  - vào vn.trip.com, chọn ngôn ngữ Tiếng Việt / tiền tệ VND
  - qua hết banner cookie, popup khuyến mãi
  - nếu gặp trang kiểm tra bot (Cloudflare / captcha) thì giải luôn tại đây
  - search thử một thành phố cho trang quen "người dùng thật"

Đóng cửa sổ là xong. Mỗi thị trường dùng profile riêng.

    python src/setup_profile.py
    python src/setup_profile.py --locale en-US --currency USD
"""
from __future__ import annotations

import asyncio
import argparse
import sys

from playwright.async_api import async_playwright

import config


async def main(locale: str, currency: str) -> None:
    profile_dir = config.profile_dir(locale, currency)
    profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Profile sẽ lưu tại: {profile_dir}")
    print("Cửa sổ Chromium đang mở. Thao tác xong thì ĐÓNG cửa sổ để lưu.\n")

    async with async_playwright() as p:
        launch_options = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            locale=locale,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        proxy = config.browser_proxy()
        if proxy:
            launch_options["proxy"] = proxy
        ctx = await p.chromium.launch_persistent_context(**launch_options)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        host = "www.trip.com" if locale.lower().startswith("en") else "vn.trip.com"
        await page.goto(f"https://{host}/hotels/", wait_until="domcontentloaded")

        # Giữ tiến trình sống cho tới khi người dùng đóng cửa sổ.
        closed = asyncio.Event()
        ctx.on("close", lambda _: closed.set())
        try:
            await closed.wait()
        except KeyboardInterrupt:
            pass

    print("\nĐã lưu profile. Chạy tiếp: python src/recon.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--locale", default=config.LOCALE)
    ap.add_argument("--currency", default=config.CURRENCY)
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args.locale, args.currency.upper()))
