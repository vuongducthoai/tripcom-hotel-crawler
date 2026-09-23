"""Crawl trang chi tiết cho các hotel đã lấy từ overview.

Chạy thử một hotel trước:
    python src/crawl_detail.py --file api_hotels_301_xxx.json --limit 1

Chạy tiếp toàn bộ (tự bỏ qua raw hotel đã thành công):
    python src/crawl_detail.py --file api_hotels_301_xxx.json

Không chạy đồng thời với crawl_api.py vì cả hai dùng chung browser_profile.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import Error as BrowserError, async_playwright

import config
import raw_store
from db.i18n import language_key
from detail_extract import PARSER_VERSION, extract_detail
from hotel_facilities import capture_hotel_facilities
from api_extract import _next_f_text

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DETAIL_DIR = config.OUTPUT_DIR / "details"
RAW_DIR = DETAIL_DIR / "raw"
DETAIL_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

MAX_RESPONSE_BYTES = 6_000_000
MAX_RESPONSES = 50

# Hotel không ra phòng sẽ được cào lại tối đa ngần này lần. Hết số lần mà vẫn
# không có thì coi như trang thật sự không có phòng, khỏi thử mãi.
MAX_ROOM_ATTEMPTS = 3

# Chờ tối đa ngần này cho getHotelRoomList sau khi đã cuộn hết trang.
ROOM_LIST_TIMEOUT_MS = 9000

# Trip.com trả thông báo chặn dưới dạng mảng byte XOR chứ không phải JSON.
ANTIBOT_XOR_KEY = 0x0A


def _decode_obfuscated(value: list) -> str | None:
    """Giải mảng byte XOR của Trip.com; None nếu không phải thông báo chặn.

    Dạng này trông như [113, 40, 108, ...] nên bộ dò cũ duyệt qua chỉ thấy
    toàn số nguyên và không nhận ra mình đang bị từ chối.
    """
    if not (50 <= len(value) <= 20_000):
        return None
    if not all(isinstance(byte, int) and 0 <= byte <= 255 for byte in value):
        return None
    text = "".join(chr(byte ^ ANTIBOT_XOR_KEY) for byte in value)
    return text if ("failedcause" in text or "Antibot" in text) else None


def _blocked_reason(value: Any) -> str | None:
    """Lý do Trip.com từ chối, None nếu response bình thường.

    Bắt cả hai dạng đã gặp thật:
      - dict có htlSpiderActionErrorCode  (vd 4030)
      - mảng byte XOR có failedcause      (vd Antibot-Gray-ip)
    """
    if isinstance(value, dict):
        if value.get("htlSpiderActionErrorCode") is not None:
            return f"htlSpiderActionErrorCode={value['htlSpiderActionErrorCode']}"
        for child in value.values():
            found = _blocked_reason(child)
            if found:
                return found
    elif isinstance(value, list):
        decoded = _decode_obfuscated(value)
        if decoded:
            try:
                return str(json.loads(decoded).get("failedcause") or "Antibot")
            except Exception:
                return "Antibot"
        for child in value:
            found = _blocked_reason(child)
            if found:
                return found
    return None


def _has_detail(value: dict) -> bool:
    return bool(
        value.get("description") or value.get("hotel_type")
        or value.get("images") or value.get("amenities") or value.get("rooms")
    )


BROWSER_CLOSED_HINTS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
)


def _browser_closed(exc: BaseException) -> bool:
    """Chromium đã đóng hẳn — khác với lỗi điều hướng một trang."""
    return any(hint in str(exc).lower() for hint in BROWSER_CLOSED_HINTS)


def _rooms_sold_out(packets: list[dict]) -> bool:
    """Trip.com có trả lời, và câu trả lời là 'hết phòng cho ngày này'.

    Khác hẳn với việc gọi hụt API: đây là dữ liệu đầy đủ, chỉ là đêm đó
    khách sạn không còn phòng bán. Không được tính là lỗi, nếu không thì
    lúc chạy lại (bỏ qua hotel đã xong) đuôi hàng đợi toàn hotel hết phòng
    và crawler sẽ tự dừng oan.
    """
    for packet in packets:
        if "getHotelRoomList" not in str(packet.get("url") or ""):
            continue
        data = packet.get("response")
        if not isinstance(data, dict):
            continue
        body = data.get("data")
        if not isinstance(body, dict):
            continue
        if body.get("isRoomListSoldOut") is True and not (body.get("roomList") or []):
            return True
    return False


def _previous_room_attempts(raw_path: Path) -> int:
    """Số lần đã thử lấy phòng cho hotel này ở các lượt chạy trước."""
    if not raw_store.exists(raw_path):
        return 0
    try:
        previous = (raw_store.read(raw_path).get("normalized") or {})
        return int(previous.get("room_attempts") or 0)
    except Exception:
        return 0


def latest_overview() -> Path:
    files = sorted(config.DATA_DIR.glob("api_hotels_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit("Không có file api_hotels_*.json trong output/data/.")
    return files[-1]


def _resolve_file(value: str | None) -> Path:
    if not value:
        return latest_overview()
    path = Path(value)
    if not path.exists():
        path = config.DATA_DIR / path.name
    if not path.exists():
        raise SystemExit(f"Không tìm thấy file overview: {value}")
    return path


def targets_from_file(value: str | None) -> tuple[list[dict], str]:
    path = _resolve_file(value)
    payload = json.loads(path.read_text(encoding="utf-8"))
    targets = []
    for hotel in payload.get("hotels") or []:
        hotel_id = hotel.get("trip_hotel_id")
        if hotel_id:
            targets.append({
                "trip_hotel_id": str(hotel_id),
                "name": hotel.get("name"),
                "address": hotel.get("address"),
                "url": hotel.get("url"),
            })
    return targets, path.name


def targets_from_db(
    locale: str = "vi-VN", *, missing_only: bool = False,
) -> tuple[list[dict], str]:
    import psycopg2

    language = language_key(locale)
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.trip_hotel_id, t.name, h.url, t.address
            FROM hotels h
            LEFT JOIN hotel_translations t
              ON t.hotel_id=h.id AND t.locale=%s
            WHERE h.trip_hotel_id IS NOT NULL
              AND (
                %s = FALSE
                OR (
                  NOT EXISTS (
                    SELECT 1
                    FROM hotel_image_categories c
                    JOIN hotel_images i ON i.id=c.hotel_image_id
                    WHERE i.hotel_id=h.id AND c.locale=%s
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM hotel_amenity_translations at
                    JOIN hotel_amenities a ON a.id=at.hotel_amenity_id
                    WHERE a.hotel_id=h.id AND at.locale=%s
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM room_type_translations rt
                    JOIN room_types r ON r.id=rt.room_type_id
                    WHERE r.hotel_id=h.id AND rt.locale=%s
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM hotel_policy_translations pt
                    JOIN hotel_policies p ON p.id=pt.hotel_policy_id
                    WHERE p.hotel_id=h.id AND pt.locale=%s
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM hotel_nearby_place_translations nt
                    JOIN hotel_nearby_places n ON n.id=nt.nearby_place_id
                    WHERE n.hotel_id=h.id AND nt.locale=%s
                  )
                )
              )
            ORDER BY h.id
            """,
            (language, missing_only, language, language, language, language, language),
        )
        rows = cur.fetchall()
    return [
        {"trip_hotel_id": str(hotel_id), "name": name, "url": url, "address": address}
        for hotel_id, name, url, address in rows
    ], "postgres"


def detail_url(
    target: dict, checkin: str, checkout: str,
    locale: str = "vi-VN", currency: str = "VND",
) -> str:
    hotel_id = target["trip_hotel_id"]
    raw = target.get("url") or f"https://vn.trip.com/hotels/detail/?hotelId={hotel_id}"
    parsed = urllib.parse.urlsplit(raw)
    host = parsed.netloc or "vn.trip.com"
    if locale.lower().startswith("en") and host.lower().endswith("trip.com"):
        host = "www.trip.com"
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({
        "hotelId": hotel_id,
        "checkIn": checkin,
        "checkOut": checkout,
        "adult": "2",
        "children": "0",
        "crn": "1",
        "curr": currency.upper(),
        "locale": locale,
    })
    return urllib.parse.urlunsplit((
        parsed.scheme or "https",
        host,
        parsed.path or "/hotels/detail/",
        urllib.parse.urlencode(query),
        "",
    ))


async def _capture_response(resp, packets: list[dict]) -> None:
    if len(packets) >= MAX_RESPONSES:
        return
    req = resp.request
    if req.resource_type not in ("xhr", "fetch"):
        return
    host = urllib.parse.urlsplit(req.url).netloc.lower()
    if "trip.com" not in host and "tripcdn.com" not in host:
        return
    try:
        body = await resp.body()
    except Exception:
        return
    if not body or len(body) > MAX_RESPONSE_BYTES:
        return
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and body[:1] not in (b"{", b"["):
        return
    try:
        value = json.loads(body)
    except Exception:
        return
    packets.append({
        "url": req.url.split("?", 1)[0],
        "method": req.method,
        "status": resp.status,
        "response": value,
    })


async def _wait_for_capture_quiet(
    tasks: set[asyncio.Task],
    packets: list[dict],
    *,
    min_wait_ms: int,
    max_wait_ms: int,
    quiet_ms: int,
) -> None:
    """Wait until captured API traffic settles, without always paying max_wait_ms."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    last_change = started
    previous_count = len(packets)
    while True:
        await asyncio.sleep(0.1)
        now = loop.time()
        current_count = len(packets)
        if current_count != previous_count:
            previous_count = current_count
            last_change = now
        elapsed_ms = (now - started) * 1000
        quiet_for_ms = (now - last_change) * 1000
        if elapsed_ms >= max_wait_ms:
            return
        if elapsed_ms >= min_wait_ms and quiet_for_ms >= quiet_ms and not tasks:
            return


def _has_room_list(packets: list[dict]) -> bool:
    return any("getHotelRoomList" in str(packet.get("url") or "") for packet in packets)


async def _wait_for_room_list(
    tasks: set[asyncio.Task], packets: list[dict], page=None,
    *, timeout_ms: int = ROOM_LIST_TIMEOUT_MS,
) -> bool:
    """Chờ getHotelRoomList xuất hiện, vừa chờ vừa nhúc nhích trang.

    Trip.com chỉ gọi API phòng khi khối phòng lọt vào viewport, và có máy/
    mạng chậm thì nó về sau nhịp cuộn cuối cả chục giây. Trả True nếu thấy.
    """
    if _has_room_list(packets):
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    nudge = 0
    while loop.time() < deadline:
        await asyncio.sleep(0.4)
        if _has_room_list(packets):
            # Thấy rồi thì chờ nốt phần thân response được ghi xong.
            await _wait_for_capture_quiet(
                tasks, packets, min_wait_ms=300, max_wait_ms=2500, quiet_ms=400
            )
            return True
        # Cứ ~1.6s lại cuộn nhẹ một nhịp để kích khối phòng.
        nudge += 1
        if page is not None and nudge % 4 == 0:
            try:
                await page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight * "
                    f"{0.55 + 0.15 * ((nudge // 4) % 3)})"
                )
            except Exception:
                return _has_room_list(packets)
    return _has_room_list(packets)


# Schema v2 cần khối hotelDetailResponse của trang (sao, tọa độ, thành phố,
# chính sách có cấu trúc…). Khối này nằm sẵn trong HTML (dữ liệu Next.js
# self.__next_f), không phải một API riêng — đọc thẳng từ trang, không tốn
# thêm request nào tới Trip.com.
DETAIL_BLOCK_URL = "embedded:hotel-detail-response"
_JSON = json.JSONDecoder()


async def _capture_detail_response(page) -> dict | None:
    try:
        text = _next_f_text(await page.content())
    except Exception:
        return None
    anchor = '"hotelDetailResponse":'
    index = text.find(anchor)
    if index < 0:
        return None
    try:
        value, _ = _JSON.raw_decode(text, index + len(anchor))
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("hotelBaseInfo") else None


def default_stay() -> tuple[str, str]:
    """Ngày ở mặc định: thứ Hai của tuần sau nữa, ở 1 đêm.

    Trước đây mặc định là "ngày mai", nên bản Việt và bản Anh cào vào hai
    ngày khác nhau sẽ có hai ngày nhận phòng khác nhau → gói giá USD và VND
    không ghép được (≈93% hotel bị lệch). Mốc thứ Hai tuần sau nữa giữ nguyên
    suốt cả tuần (luôn cách 8–14 ngày), nên cào hai thứ tiếng trong cùng tuần
    là trùng ngày.
    """
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=14)
    return monday.isoformat(), (monday + timedelta(days=1)).isoformat()


OTHER_MARKET = {"vi-VN": ("en-US", "USD"), "en-US": ("vi-VN", "VND")}


def paired_stay(hotel_id: str, locale: str, currency: str,
                checkin: str, checkout: str) -> tuple[str, str]:
    """Nếu bản thứ tiếng kia của hotel này đã cào với một ngày ở còn ở tương lai
    thì dùng lại đúng ngày đó, để hai bản ghép được gói giá với nhau."""
    other = OTHER_MARKET.get(locale)
    if not other:
        return checkin, checkout
    try:
        normalized = raw_store.read(market_raw_dir(*other) / f"{hotel_id}.json").get("normalized") or {}
    except Exception:
        return checkin, checkout
    other_in, other_out = normalized.get("check_in"), normalized.get("check_out")
    try:
        if other_in and other_out and datetime.fromisoformat(other_in).date() > datetime.now().date():
            return other_in, other_out
    except ValueError:
        pass
    return checkin, checkout


def market_raw_dir(locale: str, currency: str) -> Path:
    path = RAW_DIR / locale / currency.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _capture_hotel_policy_text(page, locale: str) -> str | None:
    """Open Trip.com's Policies panel and return the smallest matching DOM block."""
    labels = ("Policies", "Hotel policies") if locale.lower().startswith("en") else (
        "Chính Sách", "Chính sách",
    )
    clicked = False
    for label in labels:
        locator = page.get_by_text(label, exact=True)
        for index in range(await locator.count() - 1, -1, -1):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    await candidate.click(timeout=2500)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break
    if not clicked:
        return None
    await page.wait_for_timeout(800)
    phrases = (
        ["check-in and check-out", "child policies", "pets", "age requirements"]
        if locale.lower().startswith("en")
        else ["thời gian nhận và trả phòng", "chính sách cho trẻ em", "thú cưng", "giới hạn độ tuổi"]
    )
    return await page.evaluate(
        """(phrases) => {
            const visible = (el) => {
                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
            };
            const candidates = [...document.querySelectorAll('body *')]
                .filter(visible)
                .map(el => ({el, text: (el.innerText || '').trim()}))
                .filter(x => x.text.length >= 80 && x.text.length <= 20000)
                .filter(x => phrases.filter(p => x.text.toLocaleLowerCase().includes(p)).length >= 3)
                .sort((a, b) => a.text.length - b.text.length);
            return candidates.length ? candidates[0].text : null;
        }""",
        list(phrases),
    )


async def crawl_one(
    ctx, target: dict, checkin: str, checkout: str, locale: str, currency: str
) -> dict:
    hotel_id = target["trip_hotel_id"]
    url = detail_url(target, checkin, checkout, locale, currency)
    raw_path = market_raw_dir(locale, currency) / f"{hotel_id}.json"
    page = await ctx.new_page()
    packets: list[dict[str, Any]] = []
    tasks: set[asyncio.Task] = set()

    def on_response(resp) -> None:
        task = asyncio.create_task(_capture_response(resp, packets))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        await _wait_for_capture_quiet(
            tasks, packets, min_wait_ms=1200, max_wait_ms=3500, quiet_ms=500
        )
        # Kích hoạt lazy-load ảnh, tiện ích và room inventory.
        for ratio in (0.35, 0.70, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
            await _wait_for_capture_quiet(
                tasks, packets, min_wait_ms=300, max_wait_ms=900, quiet_ms=250
            )

        # Danh sách phòng nạp chậm hơn ảnh/tiện ích: cuộn xong mà chưa thấy
        # getHotelRoomList thì phải chờ thêm, nếu không sẽ ghi nhận "không có
        # phòng" trong khi thực ra chỉ là chưa kịp về.
        await _wait_for_room_list(tasks, packets, page)

        # JSON-LD thường chứa mô tả/ảnh ngay cả khi API đổi endpoint.
        for script in await page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                packets.append({"url": "embedded:json-ld", "method": "EMBEDDED", "status": 200,
                                "response": json.loads(script)})
            except Exception:
                pass

        from hotel_description import capture_hotel_description
        introduction = await capture_hotel_description(page, hotel_id)
        if introduction:
            packets.append({
                "url": "embedded:hotel-description", "method": "EMBEDDED", "status": 200,
                "response": introduction,
            })

        # Some markets keep the localized hotel introduction only in page
        # metadata rather than the captured room/facility APIs.
        meta_description = None
        for selector in ('meta[name="description"]', 'meta[property="og:description"]'):
            locator = page.locator(selector)
            if await locator.count():
                meta_description = await locator.first.get_attribute("content")
                if meta_description:
                    break
        if meta_description:
            packets.append({
                "url": "embedded:page-meta", "method": "EMBEDDED", "status": 200,
                "response": {"description": meta_description},
            })

        facilities = await capture_hotel_facilities(page)
        if facilities:
            packets.append({
                "url": "embedded:hotel-facilities", "method": "EMBEDDED", "status": 200,
                "response": facilities,
            })

        detail_block = await _capture_detail_response(page)
        if detail_block:
            packets.append({
                "url": DETAIL_BLOCK_URL, "method": "EMBEDDED", "status": 200,
                "response": detail_block,
            })

        policy_text = await _capture_hotel_policy_text(page, locale)
        if policy_text:
            packets.append({
                "url": "embedded:hotel-policies", "method": "EMBEDDED", "status": 200,
                "response": {"text": policy_text},
            })

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        normalized = extract_detail(packets, hotel_id, url, currency, locale)
        blocked = next(
            (reason for packet in packets
             if (reason := _blocked_reason(packet.get("response"))) is not None),
            None,
        )
        if blocked is None and "/account/signin" in (page.url or "").lower():
            blocked = "bị chuyển sang trang đăng nhập"
        has_detail = _has_detail(normalized)
        rooms = normalized.get("rooms") or []
        # Thiếu phòng thì CHƯA coi là xong: ảnh và tiện nghi vẫn về bình thường
        # ngay cả khi bị chặn phần phòng, nên nếu chỉ dựa vào "có dữ liệu gì đó"
        # thì hotel bị chặn sẽ được ghi nhận thành công rồi bỏ qua mãi mãi.
        sold_out = bool(not rooms and _rooms_sold_out(packets))
        room_attempts = 0 if (rooms or sold_out) else _previous_room_attempts(raw_path) + 1
        rooms_exhausted = bool(not rooms and room_attempts >= MAX_ROOM_ATTEMPTS)
        normalized.update({
            "locale": locale,
            "currency": currency,
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "rooms_missing": not bool(rooms),
            "rooms_sold_out": sold_out,
            "room_attempts": room_attempts,
            "has_detail_block": bool(detail_block),
            # Phân biệt "bị chặn / trang hỏng" với "chỉ thiếu phòng". Vòng lặp
            # ngoài dùng cờ này để quyết định có dừng cả run hay không.
            "blocked": blocked,
            "page_dead": not has_detail,
            "success": (
                has_detail and blocked is None
                and (bool(rooms) or sold_out or rooms_exhausted)
            ),
            "error": (
                f"Trip.com chặn: {blocked}" if blocked
                else "có JSON response nhưng không trích được dữ liệu detail"
                if not has_detail
                else None if sold_out
                else f"không lấy được phòng (lần {room_attempts}/{MAX_ROOM_ATTEMPTS})"
                if not rooms and not rooms_exhausted
                else None
            ),
        })
        normalized["name"] = normalized.get("name") or target.get("name")
        normalized["address"] = normalized.get("address") or target.get("address")
        raw = {"target": target, "url": url, "normalized": normalized, "responses": packets}
        # Bị chặn / trang hỏng thì KHÔNG ghi đè raw tốt của lần cào trước —
        # lưu bản hỏng sang tên .failed.<thời điểm> (giống nhánh lỗi bên dưới).
        save_path = raw_path
        if (blocked or not has_detail) and raw_store.exists(raw_path):
            try:
                if (raw_store.read(raw_path).get("normalized") or {}).get("success"):
                    save_path = raw_path.with_name(
                        f"{hotel_id}.failed.{datetime.now():%Y%m%d_%H%M%S}.json"
                    )
            except Exception:
                pass
        raw_store.write(save_path, raw)
        return normalized
    except Exception as exc:
        result = {
            "trip_hotel_id": hotel_id,
            "name": target.get("name"),
            "address": target.get("address"),
            "locale": locale,
            "currency": currency,
            "url": url,
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "images": [], "amenities": [], "rooms": [],
        }
        error_path = raw_path
        if raw_store.exists(raw_path):
            try:
                previous = raw_store.read(raw_path)
                if (previous.get("normalized") or {}).get("success"):
                    error_path = raw_path.with_name(
                        f"{hotel_id}.failed.{datetime.now():%Y%m%d_%H%M%S}.json"
                    )
            except Exception:
                pass
        raw_store.write(
            error_path,
            {"target": target, "normalized": result, "responses": packets},
        )
        return result
    finally:
        await page.close()


def _load_cached(
    hotel_id: str,
    locale: str,
    currency: str,
    *,
    allow_legacy: bool = True,
    require_detail_block: bool = False,
) -> dict | None:
    path = market_raw_dir(locale, currency) / f"{hotel_id}.json"
    legacy_path = RAW_DIR / f"{hotel_id}.json"
    if (
        allow_legacy
        and
        not raw_store.exists(path) and locale == "vi-VN" and currency.upper() == "VND"
        and raw_store.exists(legacy_path)
    ):
        path = legacy_path
    if not raw_store.exists(path):
        return None
    try:
        dump = raw_store.read(path)
        value = dump.get("normalized")
    except Exception:
        return None
    if value and value.get("success") and value.get("parser_version") != PARSER_VERSION:
        try:
            target = dump.get("target") or {}
            fresh = extract_detail(
                dump.get("responses") or [],
                str(target.get("trip_hotel_id") or hotel_id),
                dump.get("url") or value.get("url") or target.get("url") or "",
                currency,
                locale,
            )
            fresh["name"] = fresh.get("name") or value.get("name") or target.get("name")
            fresh["address"] = (
                fresh.get("address") or value.get("address") or target.get("address")
            )
            value.update(fresh)
            dump["normalized"] = value
            raw_store.write(path, dump)
        except Exception:
            return None
    if value:
        value.setdefault("locale", locale)
        value.setdefault("currency", currency)
    has_spider_error = any(
        _blocked_reason(packet.get("response")) is not None
        for packet in (dump.get("responses") or [])
    )
    # Raw ghi bởi bản code cũ có success=True dù không có phòng (lúc đó chỉ cần
    # có ảnh/tiện ích là tính xong). Không nhận lại những bản đó, nếu không
    # chúng bị bỏ qua vĩnh viễn và DB thiếu phòng mà không ai biết.
    rooms_pending = bool(
        value
        and not (value.get("rooms") or [])
        and int(value.get("room_attempts") or 0) < MAX_ROOM_ATTEMPTS
    )
    # --require-detail-block: raw cũ (chưa có khối hotelDetailResponse) coi
    # như chưa cào, để cào lại cho schema v2.
    missing_block = require_detail_block and not any(
        packet.get("url") == DETAIL_BLOCK_URL for packet in (dump.get("responses") or [])
    )
    return (
        value
        if value and value.get("success") and _has_detail(value)
        and not has_spider_error and not rooms_pending and not missing_block
        else None
    )


def save_manifest(
    path: Path, source: str, details: list[dict], complete: bool,
    locale: str, currency: str,
) -> None:
    # Full room source payloads already live in per-hotel raw captures. Keeping
    # another copy in each checkpoint made manifests hundreds of MB.
    compact_details = []
    for detail in details:
        compact = dict(detail)
        compact["rooms"] = [
            {key: value for key, value in room.items() if key != "raw"}
            for room in (detail.get("rooms") or [])
        ]
        compact_details.append(compact)
    path.write_text(json.dumps({
        "source_overview": source,
        "locale": locale,
        "currency": currency,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
        "complete": complete,
        "count": len(details),
        "success_count": sum(1 for row in details if row.get("success")),
        "details": compact_details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    locale = args.locale or config.LOCALE
    currency = (args.currency or config.CURRENCY).upper()
    targets, source = (
        targets_from_db(locale, missing_only=args.missing_only)
        if args.from_db else targets_from_file(args.file)
    )
    forced_ids: set[str] = set()
    if args.ids_file:
        # Chỉ cào đúng các id trong file (mỗi dòng một id, bỏ dòng trống và
        # dòng bắt đầu bằng #). Các id này LUÔN cào lại, bỏ qua cache — vì
        # lý do lập danh sách chính là raw cũ của chúng đang thiếu thứ gì đó.
        ids_path = Path(args.ids_file)
        if not ids_path.is_absolute() and not ids_path.exists():
            ids_path = config.ROOT / ids_path
        if not ids_path.exists():
            raise SystemExit(f"Không tìm thấy file id: {args.ids_file}")
        forced_ids = {
            line.strip() for line in ids_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        known = {str(t["trip_hotel_id"]) for t in targets}
        unknown = forced_ids - known
        targets = [t for t in targets if str(t["trip_hotel_id"]) in forced_ids]
        print(f"--ids-file: {len(forced_ids)} id, khớp {len(targets)} hotel trong nguồn"
              + (f", {len(unknown)} id không có trong nguồn (bỏ qua)" if unknown else ""))
    if args.start_after:
        targets = [t for t in targets if int(t["trip_hotel_id"]) > args.start_after]
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        raise SystemExit("Không có hotel nào để crawl detail.")
    if args.workers < 1 or args.workers > 4:
        raise SystemExit("--workers phải từ 1 đến 4; khuyến nghị 2.")
    if args.max_consecutive_errors < 1:
        raise SystemExit("--max-consecutive-errors phải lớn hơn 0.")

    default_in, default_out = default_stay()
    checkin = args.checkin or default_in
    checkout = args.checkout or (
        (datetime.fromisoformat(args.checkin) + timedelta(days=1)).strftime("%Y-%m-%d")
        if args.checkin else default_out
    )
    # Không truyền --checkin: từng hotel dùng lại ngày của bản thứ tiếng kia
    # (nếu còn ở tương lai) để hai bản ghép được gói giá.
    pair_dates = not args.checkin
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    market_tag = f"{locale}_{currency}".replace("-", "")
    out = config.DATA_DIR / f"hotel_details_{market_tag}_{stamp}.json"
    detail_slots: list[dict | None] = [None] * len(targets)

    print(
        f"Detail: {len(targets)} hotel | {locale}/{currency} | "
        f"{checkin} → {checkout} | workers={args.workers}"
    )
    print("Không chạy song song với crawl_api.py (dùng chung browser_profile).")

    # Quét cache TRƯỚC khi mở trình duyệt. Với vài nghìn file raw, bước này
    # mất nhiều phút; mở Chromium rồi để nó nằm không suốt thời gian đó chỉ
    # tổ rước rủi ro cửa sổ bị đóng/chết trước khi cào được hotel nào.
    pending: list[tuple[int, dict]] = []
    for index, target in enumerate(targets, 1):
        force = str(target["trip_hotel_id"]) in forced_ids
        cached = (
            _load_cached(
                target["trip_hotel_id"],
                locale,
                currency,
                allow_legacy=not args.ignore_legacy_cache,
                require_detail_block=args.require_detail_block,
            )
            if not (args.no_resume or force) else None
        )
        if cached:
            detail_slots[index - 1] = cached
            continue
        pending.append((index, target))
    print(f"Cache: {len(targets) - len(pending)} hotel đã có, còn {len(pending)} cần cào.")

    if not pending:
        details = [value for value in detail_slots if value is not None]
        success = sum(1 for row in details if row.get("success"))
        save_manifest(out, source, details, True, locale, currency)
        print(f"→ {success}/{len(details)} detail thành công → {out}")
        print(f"Nạp DB: python src/db/detail_loader.py {out.name}")
        return

    async with async_playwright() as p:
        profile_path = Path(args.profile_dir) if args.profile_dir else config.profile_dir(locale, currency)
        if not profile_path.is_absolute():
            profile_path = config.ROOT / profile_path
        launch_options = {
            "headless": config.HEADLESS,
            "locale": locale,
            "timezone_id": config.TIMEZONE,
            "viewport": config.VIEWPORT,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        proxy = config.browser_proxy()
        if proxy:
            launch_options["proxy"] = proxy
        if args.browser_channel:
            launch_options["channel"] = args.browser_channel
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                **launch_options,
            )
        except Exception as exc:
            raise SystemExit(
                "Không mở được browser_profile. Hãy dừng crawl_api.py/Chromium trước.\n"
                f"Chi tiết: {exc}"
            ) from exc

        async def block_heavy_resources(route) -> None:
            if route.request.resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()

        await ctx.route("**/*", block_heavy_resources)

        queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
        for job in pending:
            queue.put_nowait(job)
        crawled_count = 0
        consecutive_errors = 0
        consecutive_roomless = 0
        # Thiếu phòng KHÔNG phải dấu hiệu bị chặn: nhiều khách sạn hết phòng
        # hoặc khối phòng không nạp kịp. Chỉ dừng khi thiếu phòng kéo dài hẳn.
        roomless_limit = max(args.max_consecutive_errors * 6, 30)

        browser_gone = False

        async def worker() -> None:
            nonlocal crawled_count, consecutive_errors, consecutive_roomless
            nonlocal browser_gone
            while True:
                if browser_gone:
                    return
                try:
                    index, target = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    try:
                        stay_in, stay_out = (
                            paired_stay(str(target["trip_hotel_id"]), locale, currency,
                                        checkin, checkout)
                            if pair_dates else (checkin, checkout)
                        )
                        row = await crawl_one(
                            ctx, target, stay_in, stay_out, locale, currency
                        )
                    except BrowserError as exc:
                        if not _browser_closed(exc):
                            raise
                        # Cửa sổ Chromium đã đóng (người dùng tắt tay, hoặc nó
                        # tự chết). Không cứu được, nhưng phải dừng êm để còn
                        # ghi manifest — raw từng hotel thì đã nằm trên đĩa rồi.
                        browser_gone = True
                        print(
                            "\nTrình duyệt đã đóng giữa chừng — dừng lại và lưu "
                            "những gì đã cào.\n"
                            "  Nếu anh không tự tắt cửa sổ Chromium thì nhiều khả "
                            "năng nó hết RAM.\n"
                            "  Chạy lại lệnh cũ là nó cào tiếp từ chỗ dở."
                        )
                        return
                    detail_slots[index - 1] = row
                    crawled_count += 1
                    # Chỉ "bị chặn" hoặc "trang không ra gì" mới tính là lỗi
                    # nặng. Thiếu mỗi phòng thì đếm riêng, ngưỡng cao hơn.
                    hard_error = bool(row.get("blocked")) or bool(row.get("page_dead"))
                    if hard_error:
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 0
                    # "Hết phòng" là câu trả lời dứt khoát của Trip.com, không
                    # phải lấy hụt — không tính vào chuỗi thiếu phòng.
                    if row.get("rooms") or row.get("rooms_sold_out"):
                        consecutive_roomless = 0
                    else:
                        consecutive_roomless += 1
                    status = (
                        "LỖI" if hard_error
                        else "OK" if row.get("rooms")
                        else "HẾT PHÒNG" if row.get("rooms_sold_out")
                        else "THIẾU PHÒNG"
                    )
                    print(
                        f"[{index}/{len(targets)}] {target['trip_hotel_id']} "
                        f"{status} | "
                        f"ảnh={len(row.get('images') or [])}, "
                        f"tiện ích={len(row.get('amenities') or [])}, "
                        f"phòng={len(row.get('rooms') or [])}"
                    )
                    if crawled_count % config.CHECKPOINT_EVERY == 0:
                        checkpoint = [
                            value for value in detail_slots if value is not None
                        ]
                        save_manifest(
                            out, source, checkpoint, False, locale, currency
                        )
                        print(f"  checkpoint → {out}")
                    if consecutive_errors >= args.max_consecutive_errors:
                        print(
                            f"Dừng an toàn: {consecutive_errors} hotel liên tiếp lỗi."
                        )
                        if "chặn" in (row.get("error") or ""):
                            print(
                                f"  Nguyên nhân: {row.get('error')}\n"
                                "  Trip.com đang chặn. NGHỈ vài tiếng rồi chạy lại —\n"
                                "  thực tế đo được: nghỉ qua đêm thì tỉ lệ thành công\n"
                                "  hồi từ 14% lên 99%. Chạy cố chỉ làm nặng thêm."
                            )
                        else:
                            print("  Kiểm tra profile trình duyệt trước khi chạy tiếp.")
                        return
                    if consecutive_roomless >= roomless_limit:
                        print(
                            f"Dừng: {consecutive_roomless} hotel liên tiếp không ra "
                            "phòng, trong khi ảnh và tiện ích vẫn về bình thường.\n"
                            "  Trip.com nhiều khả năng đang giới hạn riêng API phòng.\n"
                            "  Nghỉ vài tiếng rồi chạy lại với --missing-only."
                        )
                        return
                    await asyncio.sleep(
                        random.uniform(config.MIN_DELAY, config.MAX_DELAY)
                    )
                finally:
                    queue.task_done()

        worker_count = min(args.workers, len(pending))
        if worker_count:
            await asyncio.gather(*(worker() for _ in range(worker_count)))

        # Trình duyệt chết rồi thì close() cũng ném lỗi — đừng để nó cướp mất
        # bước ghi manifest bên dưới.
        try:
            await ctx.close()
        except Exception:
            pass

    details = [value for value in detail_slots if value is not None]
    success = sum(1 for row in details if row.get("success"))
    save_manifest(out, source, details, success == len(details), locale, currency)
    print(f"→ {success}/{len(details)} detail thành công → {out}")
    print(f"Nạp DB: python src/db/detail_loader.py {out.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="file overview api_hotels_*.json; mặc định file mới nhất")
    ap.add_argument("--from-db", action="store_true", help="đọc danh sách hotel từ PostgreSQL")
    ap.add_argument(
        "--missing-only", action="store_true",
        help="với --from-db, chỉ crawl hotel chưa có detail theo ngôn ngữ",
    )
    ap.add_argument("--limit", type=int, help="chỉ crawl N hotel đầu (nên dùng 1 để kiểm thử)")
    ap.add_argument(
        "--ids-file",
        help="chỉ cào các trip_hotel_id trong file này (mỗi dòng một id), "
             "luôn cào lại bỏ qua cache — tạo bằng scripts/find_missing.py",
    )
    ap.add_argument("--start-after", type=int, help="chỉ lấy trip_hotel_id lớn hơn giá trị này")
    ap.add_argument("--checkin", help="YYYY-MM-DD")
    ap.add_argument("--checkout", help="YYYY-MM-DD")
    ap.add_argument("--locale", default=config.LOCALE, help="Trip.com locale, ví dụ vi-VN hoặc en-US")
    ap.add_argument("--currency", default=config.CURRENCY, help="Mã tiền tệ, ví dụ VND hoặc USD")
    ap.add_argument("--profile-dir", help="profile Chromium tùy chọn; mặc định tách theo market")
    ap.add_argument(
        "--browser-channel", choices=("chrome", "msedge"),
        help="dùng trình duyệt hệ thống thay cho Chrome for Testing",
    )
    ap.add_argument("--no-resume", action="store_true", help="crawl lại cả hotel đã có raw thành công")
    ap.add_argument(
        "--require-detail-block", action="store_true",
        help="coi raw chưa có khối hotelDetailResponse (raw cũ) là chưa cào — dùng khi cào lại cho schema v2",
    )
    ap.add_argument(
        "--workers", type=int, default=1,
        help="số hotel crawl song song (1-4, khuyến nghị 2)",
    )
    ap.add_argument(
        "--max-consecutive-errors", type=int, default=5,
        help="tự dừng worker sau N hotel liên tiếp lỗi (mặc định 5)",
    )
    ap.add_argument(
        "--ignore-legacy-cache",
        action="store_true",
        help=(
            "bỏ qua raw cache VI/VND đời cũ nằm trực tiếp trong output/details/raw; "
            "cache mới theo locale/currency vẫn được resume"
        ),
    )
    parsed = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(parsed))
