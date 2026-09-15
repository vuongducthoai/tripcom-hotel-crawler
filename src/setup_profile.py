"""Bước 1 — tạo Chromium profile dùng lại được.

Chạy MỘT LẦN. Script mở một cửa sổ Chromium thật, anh tự tay:
  - vào vn.trip.com, chọn ngôn ngữ Tiếng Việt / tiền tệ VND
  - qua hết banner cookie, popup khuyến mãi
  - nếu gặp trang kiểm tra bot (Cloudflare / captcha) thì giải luôn tại đây
  - search thử một thành phố cho trang quen "người dùng thật"

Đóng cửa sổ là xong. Cookie + fingerprint được lưu ở ./browser_profile/
và mọi script sau dùng lại, nên tỉ lệ bị chặn thấp hơn hẳn headless trắng.

    python src/setup_profile.py
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

import config


async def main() -> None:
    config.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Profile sẽ lưu tại: {config.PROFILE_DIR}")
    print("Cửa sổ Chromium đang mở. Thao tác xong thì ĐÓNG cửa sổ để lưu.\n")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=False,
            locale=config.LOCALE,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(config.TARGET_URLS["hotels_home"], wait_until="domcontentloaded")

        # Giữ tiến trình sống cho tới khi người dùng đóng cửa sổ.
        closed = asyncio.Event()
        ctx.on("close", lambda _: closed.set())
        try:
            await closed.wait()
        except KeyboardInterrupt:
            pass

    print("\nĐã lưu profile. Chạy tiếp: python src/recon.py")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
