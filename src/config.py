"""Cấu hình chung cho toàn bộ crawler.

Mọi giá trị nhạy cảm hoặc hay đổi đều đọc từ .env — xem .env.example.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

# ---------------------------------------------------------------- thư mục
OUTPUT_DIR = ROOT / "output"
RECON_DIR = OUTPUT_DIR / "recon"
HTML_DIR = OUTPUT_DIR / "html"
DATA_DIR = OUTPUT_DIR / "data"
PROFILE_DIR = ROOT / "browser_profile"  # Chromium user-data-dir, KHÔNG commit

for _d in (OUTPUT_DIR, RECON_DIR, HTML_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- mục tiêu
# Hai trang trong task. Sau khi search trên web, copy URL thật vào đây.
TARGET_URLS: dict[str, str] = {
    "hotels_home": "https://vn.trip.com/hotels/",
    # Thay bằng URL thật của trang "Khách sạn giá rẻ" sau khi mở trên browser
    "cheap_hotels": "https://vn.trip.com/hotels/cheap-hotels/",
    # Ví dụ trang danh sách theo thành phố — lấy URL thật từ thanh địa chỉ
    # sau khi search "Ho Chi Minh City" trên vn.trip.com
    "list_hcmc": os.getenv("TRIP_LIST_URL", "https://vn.trip.com/hotels/list?city=359"),
}

# ---------------------------------------------------------------- browser
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
LOCALE = os.getenv("LOCALE", "vi-VN")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Ho_Chi_Minh")
VIEWPORT = {"width": 1440, "height": 900}
PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))

# ---------------------------------------------------------------- tốc độ
# Giữ chậm. Bị block một lần là mất cả buổi để gỡ.
MIN_DELAY = float(os.getenv("MIN_DELAY", "1.5"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "3.5"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
MAX_SCROLL_ROUNDS = int(os.getenv("MAX_SCROLL_ROUNDS", "25"))
SCROLL_PAUSE_MS = int(os.getenv("SCROLL_PAUSE_MS", "1500"))

RESPECT_ROBOTS = os.getenv("RESPECT_ROBOTS", "true").lower() == "true"

# ---------------------------------------------------------------- database
DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": os.getenv("DB_USER", "tripcom"),
    "password": os.getenv("DB_PASSWORD", "tripcom"),
    "dbname": os.getenv("DB_NAME", "tripcom"),
}


def dsn() -> str:
    return (
        f"host={DB['host']} port={DB['port']} user={DB['user']} "
        f"password={DB['password']} dbname={DB['dbname']}"
    )
