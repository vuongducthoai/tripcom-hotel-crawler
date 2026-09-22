"""Mở vài trang Trip.com thật để kiểm tra schema v2 — CHỈ để thăm dò, không nạp DB.

    python scripts/probe_detail_v2.py                       # mặc định: 2 hotel VN + 5 thành phố nước ngoài
    python scripts/probe_detail_v2.py --hotels 134013415    # chỉ một hotel
    python scripts/probe_detail_v2.py --cities none         # bỏ phần nước ngoài

Lấy những thứ raw hiện tại CHƯA lưu:
  * khối hotelDetailResponse trong trang (thông tin cơ bản, chính sách có cấu trúc,
    tiện nghi, mô tả, vị trí, điểm nổi bật)
  * response API phòng/giá/ảnh/đánh giá/lân cận của khách sạn NƯỚC NGOÀI

Kết quả: output/probe_v2/<locale>_<hotel_id>.json.gz và list_<locale>_<city>.json.gz
Mỗi lượt chỉ vài chục trang, nghỉ 5–9 giây giữa các trang. Đừng chạy cùng lúc với
crawler khác (dùng chung browser profile).
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import random
import re
import sys
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
from api_extract import _next_f_text, extract_from_html
from crawl_detail import _blocked_reason

OUT = config.OUTPUT_DIR / "probe_v2"
# Mã thành phố Trip.com — script tự in tên thành phố trả về để xác nhận.
DEFAULT_CITIES = "Tokyo:228,Bangkok:359,Singapore:73,Seoul:274,NewYork:633"
DEFAULT_HOTELS = "134013415,110585808"
APIS = (
    "getHotelRoomListOversea", "getHotelRoomPopInfoPCOnline", "ctgethotelalbum",
    "getHotelCommentInfo", "ctGetNearbyPlaceInfo", "getDetailAdditionalInfo",
)
_decoder = json.JSONDecoder()


class Blocked(Exception):
    """Trip.com từ chối: mã 4030 hoặc chuyển sang trang đăng nhập."""


# ------------------------------------------------------------ đọc dữ liệu trong trang
def rsc_text(scripts: list[str]) -> str:
    """Ghép các chuỗi self.__next_f.push([1,"…"]) thành một văn bản (không eval)."""
    parts: list[str] = []
    for script in scripts:
        for match in re.finditer(r"self\.__next_f\.push\(\s*", script or ""):
            try:
                arg, _ = _decoder.raw_decode(script[match.end():])
            except ValueError:
                continue
            if isinstance(arg, list) and len(arg) > 1 and isinstance(arg[1], str):
                parts.append(arg[1])
    return "".join(parts)


def object_after(text: str, key: str):
    """Đọc đúng một object JSON ngay sau "key": trong văn bản RSC."""
    anchor = f'"{key}":'
    index = text.find(anchor)
    if index < 0:
        return None
    try:
        value, _ = _decoder.raw_decode(text, index + len(anchor))
    except ValueError:
        return None
    return value


def hotel_ids_in_list(list_data) -> list[str]:
    seen: list[str] = []
    for hid in re.findall(r'"hotelId":\s*"?(\d{5,})', json.dumps(list_data)):
        if hid not in seen:
            seen.append(hid)
    return seen


def city_name_in_list(list_data) -> str | None:
    found = re.search(r'"cityName":\s*"([^"]+)"', json.dumps(list_data, ensure_ascii=False))
    return found.group(1) if found else None


# ------------------------------------------------------------ trình duyệt
async def page_scripts(page) -> list[str]:
    return await page.evaluate(
        "() => [...document.scripts].map(s => s.textContent || '')"
        ".filter(s => s.includes('__next_f'))"
    )


def save(name: str, payload: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


async def open_market(p, locale: str, currency: str):
    ctx = await p.chromium.launch_persistent_context(
        user_data_dir=str(config.profile_dir(locale, currency)),
        headless=config.HEADLESS, locale=locale, timezone_id=config.TIMEZONE,
        viewport=config.VIEWPORT, args=["--disable-blink-features=AutomationControlled"],
    )

    async def block_heavy(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_heavy)
    return ctx


async def probe_list(ctx, locale, currency, city_label, city_id, checkin, checkout, per_city):
    # Cùng bộ tham số với crawl_api.build_list_url (dùng cityId, không phải city).
    host = "www.trip.com" if locale.startswith("en") else "vn.trip.com"
    params = {
        "flexType": "1", "cityId": str(city_id), "provinceId": "0", "districtId": "0",
        "searchType": "CT", "optionId": str(city_id),
        "searchValue": f"19|{city_id}*19*{city_id}",
        "checkin": checkin, "checkout": checkout, "crn": "1", "adult": "2",
        "curr": currency, "locale": locale,
    }
    url = f"https://{host}/hotels/list?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    list_bodies: list[dict] = []
    tasks: list[asyncio.Task] = []

    async def grab(response):
        if "fetchHotelList" in response.url:
            try:
                list_bodies.append(await response.json())
            except Exception:
                pass

    page = await ctx.new_page()
    page.on("response", lambda r: tasks.append(asyncio.create_task(grab(r))))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(4000)
        final_url = page.url
        html = await page.content()
        rows, meta = extract_from_html(html)          # trang 1 nằm sẵn trong HTML (SSR)
        for _ in range(3):
            if rows or list_bodies:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await page.close()
    if "signin" in final_url.lower():
        raise Blocked(f"trang danh sách {city_label} bị đẩy sang trang đăng nhập")
    ids = [r["trip_hotel_id"] for r in rows] or hotel_ids_in_list(list_bodies)
    name = next((r.get("city_name") for r in rows if r.get("city_name")), None) or city_name_in_list(list_bodies)
    save(f"list_{locale}_{city_label}", {"city_id": city_id, "city_name": name, "url": url,
                                         "total": meta.get("total"), "rows": rows, "fetchHotelList": list_bodies})
    if not ids:
        print(f"  [{city_label}] không lấy được danh sách (HTML và API đều trống)")
        return []
    print(f"  [{city_label}] cityId={city_id} → '{name}', tổng {meta.get('total')} hotel, trang 1 có {len(ids)}")
    return ids[:per_city]


async def probe_detail(ctx, locale, currency, hotel_id, checkin, checkout, label):
    host = "www.trip.com" if locale.startswith("en") else "vn.trip.com"
    url = (f"https://{host}/hotels/detail/?hotelId={hotel_id}&checkIn={checkin}&checkOut={checkout}"
           f"&adult=2&children=0&crn=1&curr={currency}&locale={locale}")
    captured: dict[str, object] = {}
    tasks: list[asyncio.Task] = []

    async def grab(response):
        name = next((a for a in APIS if a in response.url), None)
        if name and name not in captured:
            try:
                captured[name] = await response.json()
            except Exception:
                pass

    page = await ctx.new_page()
    page.on("response", lambda r: tasks.append(asyncio.create_task(grab(r))))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        for ratio in (0.3, 0.6, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
            await page.wait_for_timeout(1500)
        await page.wait_for_timeout(4000)  # chờ API phòng
        final_url = page.url
        detail = object_after(_next_f_text(await page.content()), "hotelDetailResponse")
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await page.close()

    # Cùng tiêu chí với crawl_detail: mã 4030 thật (không phải khóa rỗng) hoặc
    # bị đẩy sang trang đăng nhập.
    blocked = next((reason for v in captured.values()
                    if (reason := _blocked_reason(v)) is not None), None)
    if "signin" in final_url.lower():
        blocked = blocked or "bị đẩy sang trang đăng nhập"
    path = save(f"{locale}_{hotel_id}", {
        "hotel_id": hotel_id, "label": label, "locale": locale, "currency": currency,
        "check_in": checkin, "check_out": checkout, "url": url, "final_url": final_url,
        "hotelDetailResponse": detail, "apis": captured,
    })
    marks = " ".join(f"{a[:14]}{'✓' if a in captured else '✗'}" for a in APIS)
    print(f"  {label:10} {hotel_id:>10} detail={'✓' if detail else '✗'} {marks}"
          f"{'  ⚠ BỊ CHẶN: ' + blocked if blocked else ''}")
    return path, blocked


async def main(args) -> None:
    from playwright.async_api import async_playwright

    checkin_date = date.today() + timedelta(days=args.days_ahead)
    checkin = checkin_date.isoformat()
    checkout = (checkin_date + timedelta(days=1)).isoformat()
    vn_hotels = [h.strip() for h in args.hotels.split(",") if h.strip()]
    cities = [] if args.cities.lower() == "none" else [
        (c.split(":")[0], c.split(":")[1]) for c in args.cities.split(",") if ":" in c
    ]
    markets = [("en-US", "USD"), ("vi-VN", "VND")]
    print(f"Ngày nhận phòng {checkin} (cả hai thứ tiếng dùng CÙNG ngày để ghép gói giá)")

    foreign: list[tuple[str, str]] = []
    async with async_playwright() as p:
        for locale, currency in markets:
            print(f"\n=== {locale}/{currency} ===")
            ctx = await open_market(p, locale, currency)
            try:
                if locale == "en-US" and cities:
                    for label, city_id in cities:
                        for hid in await probe_list(ctx, locale, currency, label, city_id,
                                                    checkin, checkout, args.per_city):
                            foreign.append((label, hid))
                        await asyncio.sleep(random.uniform(5, 9))
                for label, hid in [("VN", h) for h in vn_hotels] + foreign:
                    _, blocked = await probe_detail(ctx, locale, currency, hid, checkin, checkout, label)
                    if blocked:
                        raise Blocked(blocked)
                    await asyncio.sleep(random.uniform(5, 9))
            except Blocked as why:
                print(f"\n  Trip.com đang chặn ({why}) — DỪNG để không làm nặng thêm.")
                print("  Nghỉ vài tiếng (tốt nhất qua đêm) rồi chạy lại đúng lệnh này.")
                return
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass
    print(f"\nXong. File nằm ở {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hotels", default=DEFAULT_HOTELS, help="id khách sạn VN, cách nhau dấu phẩy")
    ap.add_argument("--cities", default=DEFAULT_CITIES,
                    help="Tên:cityId,… cho thành phố nước ngoài; 'none' để bỏ qua")
    ap.add_argument("--per-city", type=int, default=2, help="số hotel lấy mỗi thành phố")
    ap.add_argument("--days-ahead", type=int, default=30, help="ngày nhận phòng = hôm nay + N")
    parsed = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(parsed))
