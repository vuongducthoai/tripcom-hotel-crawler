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
CURRENCY = os.getenv("CURRENCY", "VND")
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

# --- gọi thẳng API phân trang (nhanh hơn cuộn rất nhiều) ---
# 6546 KS / 20 mỗi trang ≈ 330 lượt gọi. Delay 0.8-1.8s → khoảng 7-10 phút/thành phố.
MAX_API_PAGES = int(os.getenv("MAX_API_PAGES", "400"))
API_PAGE_SIZE = int(os.getenv("API_PAGE_SIZE", "20"))
API_MIN_DELAY = float(os.getenv("API_MIN_DELAY", "0.8"))
API_MAX_DELAY = float(os.getenv("API_MAX_DELAY", "1.8"))
CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY", "20"))

# --- chia nhỏ truy vấn khi thành phố vượt ngưỡng chặn mềm của Trip.com ---
# Quan sát thực tế: server tự báo "hết trang" ở khoảng 3000 KS dù thành phố
# có nhiều hơn (vd TP.HCM báo 6545 nhưng crawl thẳng chỉ ra 3047). Đặt thấp
# hơn một chút (2800) cho an toàn — xem docs/recon.md mục "chặn mềm 3000".
PARTITION_CAP = int(os.getenv("PARTITION_CAP", "2800"))
# Qua chạy thực tế, truy vấn đã gắn filter có cửa sổ nhỏ hơn: khoảng 800
# hotel dù hotelTotalCount báo cao hơn. Mỗi lá phải thấp hơn mức này.
FILTERED_PARTITION_CAP = int(os.getenv("FILTERED_PARTITION_CAP", "700"))
# Crawler đọc các khoảng giá type 15 thật từ SSR rồi chia tiếp khoảng nào
# vẫn vượt cap. Không dùng type 16/23: đó là tag chồng lấn, không bao phủ
# toàn bộ thành phố dù từng mảnh vẫn phân trang tới isLastPage.
PARTITION_MAX_LEAVES = 160            # giá × sao × loại chỗ nghỉ khi cần
MIN_COMPLETE_RATIO = float(os.getenv("MIN_COMPLETE_RATIO", "0.98"))

RESPECT_ROBOTS = os.getenv("RESPECT_ROBOTS", "true").lower() == "true"

# ---------------------------------------------------------------- thành phố VN
# ID thật lấy từ response getCityList lúc recon — xem output/recon/bodies/008_*.
# Thêm/bớt tuỳ phạm vi anh Vũ chốt ở Giai đoạn 0.
VN_CITIES: list[dict] = [
    {"id": 301, "name": "TP. Hồ Chí Minh", "name_en": "Ho Chi Minh City"},
    {"id": 286, "name": "Hà Nội", "name_en": "Hanoi"},
    {"id": 1356, "name": "Đà Nẵng", "name_en": "Da Nang"},
    {"id": 1777, "name": "Nha Trang", "name_en": "Nha Trang"},
    {"id": 5204, "name": "Đà Lạt", "name_en": "Dalat"},
    {"id": 4134, "name": "Phan Thiết", "name_en": "Phan Thiet"},
    {"id": 5649, "name": "Đảo Phú Quốc", "name_en": "Phu Quoc Island"},
]

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


def profile_dir(locale: str, currency: str) -> Path:
    """Return an isolated browser profile for one locale/currency market."""
    if locale == "vi-VN" and currency.upper() == "VND":
        return PROFILE_DIR
    safe_market = f"{locale}_{currency.upper()}".replace("/", "_").replace("\\", "_")
    return ROOT / f"browser_profile_{safe_market}"
