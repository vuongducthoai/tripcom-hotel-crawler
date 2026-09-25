"""High-efficiency No-Browser Trip.com crawler.

Supports dual engines:
1. HTTPX Runner (Thoại): Replays templates or crawls SSR statically with exported browser cookies.
   Includes --chi-dump mode for mentor's translation table export.
2. Curl Cffi Async Runner (Nguyên): Chrome 124 TLS impersonation, sub-second SSR flight stream
   extraction (in-memory policies, surrounding places, rooms, ratings), proxy rotation, and DB auto-loader.

Usage:
  # Test a single hotel instantly
  python src/crawl_fast.py --hotel-id 104981087

  # Chi dump mode (description, policies, surrounding places)
  python src/crawl_fast.py --ids-file output/ids.txt --chi-dump

  # Crawl hotels from latest overview file (Direct IP: 2 workers)
  python src/crawl_fast.py --limit 10

  # Crawl with custom proxy pool and high concurrency
  python src/crawl_fast.py --proxy "http://user-session-{session_id}:pass@gate.proxy.com:7000" --concurrency 20

  # Crawl missing hotels directly from PostgreSQL and auto-import
  python src/crawl_fast.py --from-db --missing-only --apply-db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config                                   # noqa: E402
import raw_store                                # noqa: E402
import fast_api                                 # noqa: E402
from block_detect import blocked_reason as api_blocked    # noqa: E402
from api_extract import _next_f_text            # noqa: E402
from detail_extract import extract_detail       # noqa: E402

JSON_DECODER = json.JSONDecoder()
DETAIL_BLOCK_URL = "embedded:hotel-detail-response"
MARKET_HOST = {"vi-VN": "vn.trip.com", "en-US": "www.trip.com"}

BLOCK_MARKERS = (
    "htlSpiderActionErrorCode",
    "Antibot",
    "/account/signin",
    "c-slide-captcha",
    "challenge_page",
    "Antibot-Gray-ip",
)

JSON_LD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)
META_DESC = re.compile(
    r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])'
    r'[^>]+content=["\'](.*?)["\']',
    re.S | re.I,
)
SCRIPT = re.compile(r'<script\b[^>]*>(.*?)</script>', re.S | re.I)
NEXT_PUSH = re.compile(r'(?:self|window)\.__next_f\.push\(\s*')

DETAIL_DIR = config.OUTPUT_DIR / "details"
RAW_DIR = DETAIL_DIR / "raw"


# ----------------------------------------------------------------- bóc từ HTML
def detail_block(html: str) -> dict | None:
    """Lấy khối hotelDetailResponse trong flight data của Next.js."""
    try:
        text = _next_f_text(html)
    except Exception:
        return None
    anchor = '"hotelDetailResponse":'
    index = text.find(anchor)
    if index < 0:
        return None
    start = index + len(anchor)
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    try:
        value, _ = JSON_DECODER.raw_decode(text, start)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("hotelBaseInfo") else None


def json_ld_blocks(html: str) -> list[dict]:
    out = []
    for raw in JSON_LD.findall(html):
        try:
            value = json.loads(raw.strip())
        except Exception:
            continue
        out.append(value)
    return out


def meta_description(html: str) -> str | None:
    found = META_DESC.search(html)
    return found.group(1).strip() if found else None


def next_f_chunks(html: str) -> list[object]:
    """Giải các object JSON trong `self.__next_f.push` của Next.js."""
    chunks: list[object] = []
    for script in SCRIPT.findall(html):
        if "__next_f.push" not in script:
            continue
        for match in NEXT_PUSH.finditer(script):
            try:
                argument, _ = JSON_DECODER.raw_decode(script, match.end())
            except ValueError:
                continue
            payload = argument[1] if isinstance(argument, list) and len(argument) >= 2 else argument
            if isinstance(payload, (dict, list)):
                chunks.append(payload)
                continue
            if not isinstance(payload, str):
                continue
            stripped = payload.strip()
            if stripped.startswith(("{", "[")):
                try:
                    chunks.append(json.loads(stripped))
                except ValueError:
                    pass
            for line in payload.splitlines():
                rest = line.strip().split(":", 1)[-1]
                if not rest.startswith(("{", "[")):
                    continue
                try:
                    chunks.append(json.loads(rest))
                except ValueError:
                    pass
    return chunks


def room_payloads(value: object) -> list[dict]:
    """Tìm payload phòng tĩnh lồng sâu trong cây SSR."""
    found: list[dict] = []
    if isinstance(value, dict):
        if "physicRoomMap" in value or "roomPopInfo" in value:
            return [value]
        for child in value.values():
            found.extend(room_payloads(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(room_payloads(child))
    return found


def blocked_reason(html: str, final_url: str) -> str | None:
    if "/account/signin" in (final_url or "").lower():
        return "bị chuyển sang trang đăng nhập"
    for marker in BLOCK_MARKERS:
        if marker in html:
            return f"HTML chứa dấu hiệu chặn: {marker}"
    return None


def facilities_from_detail(detail: dict | None) -> dict | None:
    """hotelFacilityPopV2 trong SSR → khối 'embedded:hotel-facilities'."""
    pop = (detail or {}).get("hotelFacilityPopV2")
    if not isinstance(pop, dict):
        return None

    items: list[dict] = []
    seen: set[int] = set()

    def them(muc: dict, ten_nhom: str = "", ma_nhom=None) -> None:
        code = muc.get("code")
        if not isinstance(code, int) or code in seen:
            return
        seen.add(code)
        items.append({
            "name": muc.get("facilityDesc"),
            "code": code,
            "category": ten_nhom or None,
            "category_code": ma_nhom,
            "fee_label": muc.get("showTitle") or None,
            "additional_info": muc.get("facilityInfo") or [],
            "is_highlight": False,
            "is_available": True,
        })

    goc = pop.get("hotelPopularFacility") or {}
    for muc in goc.get("list") or []:
        them(muc, goc.get("title") or "", goc.get("categoryId"))

    for nhom in pop.get("hotelFacility") or []:
        ten, ma = nhom.get("title") or "", nhom.get("categoryId")
        for cum in nhom.get("categoryList") or []:
            for muc in cum.get("list") or []:
                them(muc, ten, ma)

    for muc in pop.get("hotelNormalFacilityList") or []:
        them(muc)

    return {"items": items, "source": "ssr:hotelFacilityPopV2"} if items else None


def packets_from_html(html: str) -> list[dict]:
    """Dựng danh sách packet đúng định dạng raw mà v2_loader đang đọc."""
    packets: list[dict] = []
    block = detail_block(html)
    if block:
        packets.append({
            "url": DETAIL_BLOCK_URL,
            "method": "EMBEDDED",
            "status": 200,
            "response": block,
        })
        tien_nghi = facilities_from_detail(block)
        if tien_nghi:
            packets.append({
                "url": "embedded:hotel-facilities",
                "method": "EMBEDDED",
                "status": 200,
                "response": tien_nghi,
            })
    for value in json_ld_blocks(html):
        packets.append({
            "url": "embedded:json-ld",
            "method": "EMBEDDED",
            "status": 200,
            "response": value,
        })
    desc = meta_description(html)
    if desc:
        packets.append({
            "url": "embedded:page-meta",
            "method": "EMBEDDED",
            "status": 200,
            "response": {"description": desc},
        })
    seen_rooms: set[str] = set()
    for chunk in next_f_chunks(html):
        for payload in room_payloads(chunk):
            signature = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if signature in seen_rooms:
                continue
            seen_rooms.add(signature)
            packets.append({
                "url": "embedded:hotel-rooms",
                "method": "EMBEDDED",
                "status": 200,
                "response": payload,
            })
    return packets


# ------------------------------------------------------------------- cookie
def cookie_file(locale: str, currency: str) -> Path:
    return config.OUTPUT_DIR / f"cookies_{locale}_{currency.upper()}.json"


async def export_cookies(locale: str, currency: str) -> Path:
    """Mở browser_profile đúng một lần để lấy cookie, rồi đóng."""
    from playwright.async_api import async_playwright

    target = cookie_file(locale, currency)
    async with async_playwright() as pw:
        launch_options: dict[str, Any] = {"headless": True}
        proxy = config.browser_proxy()
        if proxy:
            launch_options["proxy"] = proxy
        context = await pw.chromium.launch_persistent_context(
            str(config.profile_dir(locale, currency)), **launch_options
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                f"https://{MARKET_HOST.get(locale, 'www.trip.com')}/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            cookies = await context.cookies()
        finally:
            await context.close()
    target.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    return target


def load_cookies(locale: str, currency: str) -> dict:
    path = cookie_file(locale, currency)
    if not path.exists():
        raise SystemExit(
            f"Chưa có cookie: {path}\n→ chạy: python src/crawl_fast.py --export-cookies"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in data if c.get("name")}


# ------------------------------------------------------------------- tải trang
def detail_url(
    hotel_id: str, locale: str, currency: str, checkin: str, checkout: str
) -> str:
    host = MARKET_HOST.get(locale, "www.trip.com")
    return (
        f"https://{host}/hotels/detail/?hotelId={hotel_id}"
        f"&checkIn={checkin}&checkOut={checkout}"
        f"&adult=2&children=0&crn=1&curr={currency.upper()}&locale={locale}"
    )


def headers_for(locale: str) -> dict:
    return {
        "User-Agent": (
            config.USER_AGENT
            if hasattr(config, "USER_AGENT")
            else (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": (
            "vi-VN,vi;q=0.9,en;q=0.8" if locale.startswith("vi") else "en-US,en;q=0.9"
        ),
        "Cache-Control": "no-cache",
    }


def dem_phan_dump(dump: dict, n: dict) -> tuple[int, int, int]:
    """Đếm 3 phần mà file dump cần: mô tả (ký tự), chính sách (mục), lân cận (địa điểm)."""
    detail, gan = None, None
    for goi in dump.get("responses") or []:
        url = str(goi.get("url") or "")
        if url == DETAIL_BLOCK_URL:
            detail = goi.get("response")
        elif "ctGetNearbyPlaceInfo" in url:
            gan = goi.get("response")

    policy = ((detail or {}).get("hotelPolicyInfo")) or {}
    so_muc = sum(
        1
        for v in policy.values()
        if isinstance(v, dict) and v.get("title") and v.get("content")
    )

    nhom = ((gan or {}).get("data") or gan or {}).get("placeInfoList") or []
    so_lan_can = sum(len(g.get("places") or []) for g in nhom if isinstance(g, dict))
    if not so_lan_can:
        so_lan_can = len(n.get("nearby_places") or [])
    return len(n.get("description") or ""), so_muc, so_lan_can


def fetch_one(
    client,
    hotel_id: str,
    locale: str,
    currency: str,
    checkin: str,
    checkout: str,
    save_html: bool = False,
    mau: dict | None = None,
    visitor_id: str | None = None,
    chi_dump: bool = False,
) -> tuple[dict, str | None]:
    """Trả về (dump raw, lý do bị chặn nếu có)."""
    url = detail_url(hotel_id, locale, currency, checkin, checkout)
    response = client.get(url, headers=headers_for(locale), follow_redirects=True)
    html = response.text
    if save_html:
        folder = config.OUTPUT_DIR / "html"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"detail_{hotel_id}_{locale}.html"
        path.write_text(html, encoding="utf-8")
        print(f"       HTML đã lưu → {path} ({len(html):,} ký tự)")
    reason = blocked_reason(html, str(response.url))

    packets = [] if reason else packets_from_html(html)

    if packets and mau:
        detail = next(
            (p["response"] for p in packets if p["url"] == DETAIL_BLOCK_URL), None
        )
        ctx = fast_api.context_from_detail(detail)
        can_goi = sorted(mau.get("apis") or {})
        if chi_dump:
            can_goi = [t for t in can_goi if t == "ctGetNearbyPlaceInfo"]
        for ten_api in can_goi:
            try:
                api_url, api_headers, body = fast_api.payload_for(
                    mau,
                    ten_api,
                    hotel_id,
                    checkin,
                    checkout,
                    ctx,
                    visitor_id=visitor_id,
                )
                r = client.post(
                    api_url,
                    json=body,
                    headers={**headers_for(locale), **api_headers},
                )
                value = r.json()
            except Exception as exc:
                print(f"       API {ten_api} lỗi: {type(exc).__name__}: {exc}")
                continue
            vi_sao = api_blocked(value)
            if vi_sao:
                reason = f"API {ten_api} bị chặn: {vi_sao}"
                packets = []
                break
            packets.append({
                "url": api_url.split("?", 1)[0],
                "method": "POST",
                "status": r.status_code,
                "response": value,
            })

    normalized = extract_detail(packets, hotel_id, url, currency, locale)
    normalized["success"] = bool(packets) and reason is None
    normalized["check_in"], normalized["check_out"] = checkin, checkout
    normalized["crawled_at"] = datetime.now().isoformat(timespec="seconds")
    normalized["locale"], normalized["currency"] = locale, currency
    normalized["data_mode"] = "api-enriched" if mau else "ssr-static"
    normalized["api_enriched"] = bool(mau)
    normalized["rooms_live"] = any(
        "getHotelRoomListOversea" in str(packet.get("url") or "") for packet in packets
    )
    if reason:
        normalized["error"] = f"Trip.com chặn: {reason}"
    elif not packets:
        normalized["error"] = "HTML không có flight data hotelDetailResponse"

    return {
        "target": {"hotel_id": hotel_id, "url": url},
        "url": url,
        "normalized": normalized,
        "responses": packets,
    }, reason


def save(
    dump: dict,
    hotel_id: str,
    locale: str,
    currency: str,
    into_raw: bool = False,
) -> tuple[Path, bool]:
    """Ghi raw. Trả về (đường dẫn, có bị bỏ qua vì raw cũ đầy đủ hơn không)."""
    root = "raw" if into_raw else "raw_fast"
    folder = config.OUTPUT_DIR / "details" / root / locale / currency.upper()
    folder.mkdir(parents=True, exist_ok=True)
    ok = (dump.get("normalized") or {}).get("success")
    target = folder / f"{hotel_id}.json"

    if ok and into_raw:
        old_quality = _raw_quality(target)
        new_quality = _dump_quality(dump)
        if old_quality > new_quality:
            return target, True

    name = (
        f"{hotel_id}.json"
        if ok
        else f"{hotel_id}.failed.{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    return raw_store.write(folder / name, dump), False


def _dump_quality(dump: dict) -> tuple[int, int, int, int, int]:
    """Xếp raw theo: thành công → CÓ KHỐI DETAIL → API phòng → offer → số packet."""
    normalized = dump.get("normalized") or {}
    responses = dump.get("responses") or []
    if not normalized.get("success") or any(
        api_blocked(packet.get("response")) for packet in responses
    ):
        return (0, 0, 0, 0, 0)
    co_detail = any(packet.get("url") == DETAIL_BLOCK_URL for packet in responses)
    room_packets = [
        packet
        for packet in responses
        if "getHotelRoomListOversea" in str(packet.get("url") or "")
    ]
    has_offers = False
    for packet in room_packets:
        response = packet.get("response") or {}
        data = response.get("data") or {} if isinstance(response, dict) else {}
        if data.get("saleRoomMap"):
            has_offers = True
            break
    return (1, int(co_detail), int(bool(room_packets)), int(has_offers), len(responses))


def _raw_quality(path: Path) -> tuple[int, int, int, int, int]:
    """Chất lượng raw đang có; raw API không bị SSR tĩnh ghi đè."""
    try:
        return _dump_quality(raw_store.read(path))
    except Exception:
        return (0, 0, 0, 0, 0)


def default_stay() -> tuple[str, str]:
    """Thứ Hai của tuần sau nữa, ở 1 đêm."""
    from datetime import date, timedelta
    hom_nay = date.today()
    thu_hai = hom_nay + timedelta(days=(7 - hom_nay.weekday()) % 7 or 7) + timedelta(days=7)
    return thu_hai.isoformat(), (thu_hai + timedelta(days=1)).isoformat()


def read_ids(args) -> list[str]:
    if getattr(args, "hotel_id", None):
        return [str(args.hotel_id)]
    if getattr(args, "ids_file", None):
        text = Path(args.ids_file).read_text(encoding="utf-8")
        return [x for x in re.split(r"\s+", text.strip()) if x and not x.startswith("#")]
    if getattr(args, "file", None):
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        return [str(h["trip_hotel_id"]) for h in payload.get("hotels") or []]
    if getattr(args, "from_db", False):
        from crawl_detail import targets_from_db
        targets, _ = targets_from_db(
            locale=getattr(args, "locale", "vi-VN") or "vi-VN",
            missing_only=getattr(args, "missing_only", False),
        )
        return [str(t["trip_hotel_id"]) for t in targets]
    raise SystemExit("Cần --hotel-id, --ids-file, --file hoặc --from-db")


# ---------------------------------------------------------------------- Async Engine (curl_cffi)
def _load_cached_fast(
    hotel_id: str,
    locale: str,
    currency: str,
    allow_legacy: bool = True,
    require_detail_block: bool = False,
) -> dict | None:
    from crawl_detail import market_raw_dir
    raw_path = market_raw_dir(locale, currency) / f"{hotel_id}.json"
    legacy_path = RAW_DIR / f"{hotel_id}.json"
    if allow_legacy and not raw_store.exists(raw_path) and locale == "vi-VN" and currency.upper() == "VND":
        if raw_store.exists(legacy_path):
            raw_path = legacy_path

    if not raw_store.exists(raw_path):
        return None
    try:
        data = raw_store.read(raw_path)
        normalized = data.get("normalized")
        if not (normalized and normalized.get("success")):
            return None
        if require_detail_block and not any(
            packet.get("url") == DETAIL_BLOCK_URL for packet in (data.get("responses") or [])
        ):
            return None
        return normalized
    except Exception:
        return None


async def crawl_one_fast(
    client,
    target: dict,
    checkin: str,
    checkout: str,
    locale: str,
    currency: str,
    include_rooms: bool = False,
) -> dict:
    from crawl_detail import market_raw_dir
    from ssr_extractor import parse_hotel_html

    hotel_id = str(target["trip_hotel_id"])
    url = target.get("detail_url") or target.get("url") or detail_url(hotel_id, locale, currency, checkin, checkout)

    start_t = time.monotonic()
    html, fetch_err = await client.get_hotel_page(url, hotel_id=hotel_id)
    fetch_sec = round(time.monotonic() - start_t, 2)

    if fetch_err or not html:
        fail_row = {
            "trip_hotel_id": hotel_id,
            "hotel_name": target.get("name") or target.get("hotel_name", ""),
            "detail_url": url,
            "success": False,
            "error": fetch_err or "Empty response",
            "fetch_time_sec": fetch_sec,
            "crawled_at": datetime.now().isoformat(),
        }
        fail_dump = {
            "target": target,
            "url": url,
            "normalized": fail_row,
            "responses": [],
        }
        folder = market_raw_dir(locale, currency)
        folder.mkdir(parents=True, exist_ok=True)
        raw_store.write(
            folder / f"{hotel_id}.failed.{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            fail_dump,
        )
        return fail_row

    parsed = parse_hotel_html(
        html_text=html,
        hotel_id=hotel_id,
        url=url,
        currency=currency,
        locale=locale,
    )
    normalized = parsed["normalized"]
    packets = parsed.get("packets") or []
    normalized["fetch_time_sec"] = fetch_sec
    normalized["hotel_name"] = normalized.get("name") or target.get("name") or target.get("hotel_name", "")
    normalized["check_in"] = checkin
    normalized["check_out"] = checkout
    normalized["crawled_at"] = datetime.now().isoformat(timespec="seconds")
    normalized["success"] = bool(normalized.get("name") or normalized.get("images") or normalized.get("amenities"))
    normalized["rooms_missing"] = not bool(normalized.get("rooms"))

    folder = market_raw_dir(locale, currency)
    folder.mkdir(parents=True, exist_ok=True)
    raw_path = folder / f"{hotel_id}.json"
    raw_store.write(raw_path, {
        "target": target,
        "url": url,
        "normalized": normalized,
        "responses": packets,
    })

    return normalized


async def run_curl_async(args) -> int:
    from crawl_detail import (
        paired_stay,
        save_manifest,
        targets_from_db,
        targets_from_file,
    )
    from engine.http_client_v2 import FastHttpClient

    locale = args.locale or config.LOCALE
    currency = args.currency or config.CURRENCY
    concurrency = args.concurrency or (15 if args.proxy else 2)
    proxy = args.proxy

    forced_ids: set[str] = set()
    if args.ids_file:
        path = Path(args.ids_file)
        if path.exists():
            forced_ids = {
                line.strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")
            }

    source = ""
    targets: list[dict] = []
    if args.hotel_id:
        source = f"single:{args.hotel_id}"
        targets = [{"trip_hotel_id": str(args.hotel_id), "hotel_name": "", "detail_url": None}]
    elif args.file:
        targets, source = targets_from_file(args.file)
    elif args.from_db:
        targets, source = targets_from_db(locale, missing_only=args.missing_only)
    elif args.ids_file and forced_ids:
        source = f"ids_file:{args.ids_file}"
        targets = [{"trip_hotel_id": hid, "hotel_name": "", "detail_url": None} for hid in sorted(forced_ids)]
    else:
        candidates = sorted(config.DATA_DIR.glob("api_hotels_*.json"), key=os.path.getmtime, reverse=True)
        if candidates:
            targets, source = targets_from_file(str(candidates[0]))
            print(f"Chọn tự động file tổng quan mới nhất: {source}")
        else:
            print("Không tìm thấy file tổng quan api_hotels_*.json và không có cờ --from-db.")
            return 1

    if forced_ids and not args.hotel_id and source != f"ids_file:{args.ids_file}":
        targets = [t for t in targets if str(t["trip_hotel_id"]) in forced_ids]

    if args.start_after:
        targets = [t for t in targets if int(t["trip_hotel_id"]) > args.start_after]
    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("Không có khách sạn nào cần cào.")
        return 0

    default_in, default_out = default_stay()
    checkin = args.checkin or default_in
    checkout = args.checkout or (
        (datetime.fromisoformat(args.checkin) + timedelta(days=1)).strftime("%Y-%m-%d")
        if args.checkin else default_out
    )
    pair_dates = not args.checkin
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    market_tag = f"{locale}_{currency}".replace("-", "")
    out = config.DATA_DIR / f"hotel_details_{market_tag}_{stamp}.json"

    delay_min = args.delay_min if getattr(args, "delay_min", None) is not None else getattr(args, "delay", config.MIN_DELAY)
    delay_max = args.delay_max if getattr(args, "delay_max", None) is not None else (delay_min + getattr(args, "jitter", 1.5))

    print(
        f"=== FAST HTTP CRAWLER (No-Browser / curl_cffi) ===\n"
        f"Mục tiêu: {len(targets)} khách sạn | Thị trường: {locale}/{currency}\n"
        f"Ngày ở: {checkin} → {checkout} (pair_dates={pair_dates})\n"
        f"Chế độ mạng: {'ROTATING PROXY' if proxy else 'DIRECT IP (Subcritical Safe)'}\n"
        f"Concurrency: {concurrency} workers | Delay: {delay_min}s - {delay_max}s"
    )

    detail_slots: list[dict | None] = [None] * len(targets)
    pending: list[tuple[int, dict]] = []

    for index, target in enumerate(targets, start=1):
        force = str(target["trip_hotel_id"]) in forced_ids
        if not (getattr(args, "no_resume", False) or force):
            cached = _load_cached_fast(
                str(target["trip_hotel_id"]),
                locale,
                currency,
                require_detail_block=getattr(args, "require_detail_block", False),
            )
            if cached:
                detail_slots[index - 1] = cached
                continue
        pending.append((index, target))

    print(f"Cache: {len(targets) - len(pending)} đã có sẵn, {len(pending)} cần cào mới.\n")

    if not pending:
        details = [v for v in detail_slots if v is not None]
        save_manifest(out, source, details, True, locale, currency)
        print(f"Toàn bộ đã có trong cache -> {out}")
        return 0

    client = FastHttpClient(
        base_proxy=proxy,
        min_delay=delay_min,
        max_delay=delay_max,
    )

    queue: asyncio.Queue[tuple[int, dict]] = asyncio.Queue()
    for item in pending:
        queue.put_nowait(item)

    crawled_count = 0
    consecutive_errors = 0
    start_time = time.monotonic()

    async def worker() -> None:
        nonlocal crawled_count, consecutive_errors
        while True:
            try:
                index, target = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            stay_in, stay_out = (
                paired_stay(str(target["trip_hotel_id"]), locale, currency, checkin, checkout)
                if pair_dates else (checkin, checkout)
            )
            row = await crawl_one_fast(
                client=client,
                target=target,
                checkin=stay_in,
                checkout=stay_out,
                locale=locale,
                currency=currency,
                include_rooms=getattr(args, "include_rooms", False),
            )

            detail_slots[index - 1] = row
            crawled_count += 1

            is_ok = bool(row.get("success"))
            if not is_ok:
                consecutive_errors += 1
            else:
                consecutive_errors = 0

            status = "OK" if is_ok else "LỖI"
            sec = row.get("fetch_time_sec", 0)
            img_c = len(row.get("images") or [])
            amen_c = len(row.get("amenities") or [])
            room_c = len(row.get("rooms") or [])
            pol_c = len(row.get("policies") or [])
            place_c = len(row.get("nearby_places") or [])

            print(
                f"[{index}/{len(targets)}] {target['trip_hotel_id']} {status} | "
                f"ảnh={img_c}, tiện ích={amen_c}, phòng={room_c}, chính sách={pol_c}, lân cận={place_c} ({sec}s)"
            )

            if crawled_count % config.CHECKPOINT_EVERY == 0:
                ckpt = [v for v in detail_slots if v is not None]
                save_manifest(out, source, ckpt, False, locale, currency)

            max_consec = getattr(args, "max_consecutive_errors", 5)
            if not proxy and consecutive_errors >= max_consec:
                print(
                    f"\nDừng an toàn: {consecutive_errors} lỗi liên tiếp trên Direct IP.\n"
                    f"Trip.com có dấu hiệu siết rate-limit. Nghỉ giải nhiệt trước khi chạy tiếp."
                )
                return

    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers)

    total_time = time.monotonic() - start_time
    details = [v for v in detail_slots if v is not None]
    save_manifest(out, source, details, True, locale, currency)

    success_c = sum(1 for d in details if d.get("success"))
    speed = round(crawled_count / max(0.001, total_time), 2)

    print(
        f"\n=== HOÀN THÀNH ===\n"
        f"Thành công: {success_c}/{len(details)} khách sạn\n"
        f"Thời gian: {round(total_time, 2)}s | Tốc độ: {speed} khách sạn/giây\n"
        f"Dữ liệu xuất tại: {out}"
    )

    if getattr(args, "apply_db", False):
        print(f"\nĐang nạp vào cơ sở dữ liệu PostgreSQL...")
        subprocess.run([sys.executable, "src/db/detail_loader.py", str(out)], check=False)

    return 0 if success_c else 1


# ---------------------------------------------------------------------- Runner HTTPX
def run_httpx(args) -> int:
    import httpx

    locale = args.locale or "vi-VN"
    currency = args.currency or "VND"
    concurrency = args.concurrency or 1

    if getattr(args, "chi_dump", False):
        args.enrich_apis = True
    mau = fast_api.load_templates(locale, currency) if getattr(args, "enrich_apis", False) else None
    if getattr(args, "enrich_apis", False) and mau:
        if getattr(args, "chi_dump", False):
            print("Chế độ CHỈ DUMP: 1 lượt tải trang + 1 API ctGetNearbyPlaceInfo (bỏ phòng/giá/album)")
        else:
            print(f"Dùng mẫu API của khách sạn {mau['hotel_id']} ({len(mau.get('apis') or {})} API)")
    elif getattr(args, "enrich_apis", False):
        raise SystemExit("Chưa có mẫu API. Tạo bằng --build-templates <HOTEL_ID>.")
    else:
        print("Chế độ SSR tĩnh: không gọi API phòng/giá; dùng --enrich-apis cho lượt enrichment riêng.")

    ids = read_ids(args)
    if args.limit:
        ids = ids[:args.limit]
    checkin = args.checkin or default_stay()[0]
    checkout = args.checkout or default_stay()[1]
    delay = getattr(args, "delay", 2.0)
    jitter = getattr(args, "jitter", 1.5)

    print(f"Crawl tĩnh (httpx): {len(ids)} khách sạn | {locale}/{currency.upper()} "
          f"| {checkin} → {checkout} | {concurrency} luồng, "
          f"nghỉ {delay}–{delay + jitter:.1f}s mỗi lượt")

    cookies = load_cookies(locale, currency)
    dung = threading.Event()
    khoa = threading.Lock()
    dem = {"xong": 0, "hong": 0, "bo_qua": 0}
    ly_do_chan: list[str] = []

    def lam_mot(index: int, hotel_id: str, client) -> None:
        if dung.is_set():
            return
        time.sleep(random.uniform(delay, delay + jitter))
        if dung.is_set():
            return
        try:
            dump, reason = fetch_one(
                client,
                hotel_id,
                locale,
                currency,
                checkin,
                checkout,
                save_html=getattr(args, "save_html", False),
                mau=mau,
                visitor_id=cookies.get("UBT_VID"),
                chi_dump=getattr(args, "chi_dump", False),
            )
        except Exception as exc:
            with khoa:
                dem["hong"] += 1
                print(f"  [{index}/{len(ids)}] {hotel_id} LỖI MẠNG | {exc}")
            return

        path, bo_qua = save(
            dump,
            hotel_id,
            locale,
            currency,
            into_raw=getattr(args, "into_raw", False),
        )
        n = dump["normalized"]
        with khoa:
            if reason:
                ly_do_chan.append(reason)
                dung.set()
                print(f"  [{index}/{len(ids)}] {hotel_id} BỊ CHẶN | {reason}")
            elif bo_qua:
                dem["bo_qua"] += 1
                print(f"  [{index}/{len(ids)}] {hotel_id} BỎ QUA | raw cũ đầy đủ hơn")
            elif n.get("success"):
                dem["xong"] += 1
                if getattr(args, "chi_dump", False):
                    mo_ta, so_policy, so_lan_can = dem_phan_dump(dump, n)
                    print(f"  [{index}/{len(ids)}] {hotel_id} OK | mô tả={mo_ta} ký tự, "
                          f"chính sách={so_policy}, lân cận={so_lan_can} → {path.name}")
                else:
                    print(f"  [{index}/{len(ids)}] {hotel_id} OK | ảnh={len(n.get('images') or [])}, "
                          f"tiện ích={len(n.get('amenities') or [])}, "
                          f"phòng={len(n.get('rooms') or [])} → {path.name}")
            else:
                dem["hong"] += 1
                print(f"  [{index}/{len(ids)}] {hotel_id} THIẾU | {n.get('error')}")

    client_options: dict[str, Any] = {
        "cookies": cookies,
        "timeout": getattr(args, "timeout", 30.0),
        "http2": True,
    }
    proxy_url = config.httpx_proxy_url()
    if proxy_url:
        client_options["proxy"] = proxy_url
        print("Proxy: đang bật cho toàn bộ request HTTP")

    with httpx.Client(**client_options) as client:
        if concurrency <= 1:
            for index, hotel_id in enumerate(ids, 1):
                lam_mot(index, hotel_id, client)
                if dung.is_set():
                    break
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                viec = [pool.submit(lam_mot, i, h, client) for i, h in enumerate(ids, 1)]
                for v in viec:
                    v.result()

    done, failed = dem["xong"], dem["hong"]
    if ly_do_chan:
        print("\n  Dừng lại. Trip.com đang chặn — nghỉ vài tiếng rồi chạy lại đúng lệnh này.")
        print(f"  Đã xong trước khi dừng: {done} khách sạn.")
        return 2

    print(f"\nXong: {done} thành công, {failed} hỏng.")
    return 0 if done else 1


def main(args) -> int:
    if getattr(args, "export_cookies", False):
        path = asyncio.run(export_cookies(args.locale or "vi-VN", args.currency or "VND"))
        print(f"Đã lưu cookie → {path}")
        return 0

    if getattr(args, "build_templates", None):
        locale = args.locale or "vi-VN"
        currency = args.currency or "VND"
        folder = config.OUTPUT_DIR / "details" / "raw" / locale / currency.upper()
        path, apis = fast_api.build_templates(
            folder / f"{args.build_templates}.json", locale, currency
        )
        print(f"Đã dựng mẫu {len(apis)} API → {path}")
        for a in apis:
            print("   -", a)
        return 0

    engine = getattr(args, "engine", "auto")
    use_httpx = False
    if engine == "httpx":
        use_httpx = True
    elif engine == "curl":
        use_httpx = False
    else:  # auto
        if (
            getattr(args, "chi_dump", False)
            or getattr(args, "enrich_apis", False)
            or getattr(args, "into_raw", False)
            or getattr(args, "save_html", False)
        ):
            use_httpx = True
        elif (
            getattr(args, "proxy", None)
            or getattr(args, "from_db", False)
            or getattr(args, "missing_only", False)
            or getattr(args, "apply_db", False)
            or getattr(args, "include_rooms", False)
        ):
            use_httpx = False
        else:
            cookie_p = cookie_file(args.locale or "vi-VN", args.currency or "VND")
            use_httpx = cookie_p.exists()

    if use_httpx:
        return run_httpx(args)
    else:
        return asyncio.run(run_curl_async(args))


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="High-efficiency No-Browser Trip.com crawler")
    ap.add_argument("--hotel-id", help="Cào thử riêng 1 khách sạn theo ID")
    ap.add_argument("--ids-file", help="File danh sách trip_hotel_id, mỗi dòng một id")
    ap.add_argument("--file", help="Đường dẫn file overview JSON")
    ap.add_argument("--city-id", type=int, help="City ID")
    ap.add_argument("--limit", type=int, help="Giới hạn số khách sạn cần cào")
    ap.add_argument("--start-after", type=int, help="Bỏ qua các ID nhỏ hơn")
    ap.add_argument("--locale", default=config.LOCALE)
    ap.add_argument("--currency", default=config.CURRENCY)
    ap.add_argument("--checkin", help="YYYY-MM-DD")
    ap.add_argument("--checkout", help="YYYY-MM-DD")
    ap.add_argument("--concurrency", type=int, default=1, help="Số worker đồng thời")
    ap.add_argument("--delay", type=float, default=2.0, help="Giây nghỉ giữa 2 khách sạn")
    ap.add_argument("--jitter", type=float, default=1.5, help="Ngẫu nhiên thêm 0..N giây")
    ap.add_argument("--delay-min", type=float, help="Độ trễ tối thiểu (curl runner)")
    ap.add_argument("--delay-max", type=float, help="Độ trễ tối đa (curl runner)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--proxy", help="URL proxy hoặc template proxy xoay tua")
    ap.add_argument("--from-db", action="store_true", help="Lấy danh sách từ DB hotels")
    ap.add_argument("--missing-only", action="store_true", help="Chỉ cào khách sạn thiếu dữ liệu trong DB")
    ap.add_argument("--no-resume", action="store_true", help="Cào lại từ đầu, bỏ qua cache raw")
    ap.add_argument("--apply-db", action="store_true", help="Tự động nạp vào DB sau khi cào xong")
    ap.add_argument("--include-rooms", action="store_true", help="Thu thập thêm phòng nếu có")
    ap.add_argument("--max-consecutive-errors", type=int, default=5, help="Số lỗi liên tiếp tối đa trước khi dừng")
    ap.add_argument("--require-detail-block", action="store_true", help="Coi raw chưa có khối hotelDetailResponse là chưa cào")
    ap.add_argument("--build-templates", metavar="HOTEL_ID", help="Dựng mẫu API từ raw của khách sạn này")
    ap.add_argument("--chi-dump", action="store_true", help="Chỉ lấy những gì file dump cần (mô tả, chính sách, lân cận)")
    ap.add_argument("--enrich-apis", action="store_true", help="Gọi lại API trong template để lấy giá/offers/nearby")
    ap.add_argument("--save-html", action="store_true", help="Lưu HTML thô vào output/html")
    ap.add_argument("--into-raw", action="store_true", help="Ghi thẳng vào output/details/raw")
    ap.add_argument("--export-cookies", action="store_true", help="Mở browser_profile 1 lần để lấy cookie phiên đăng nhập")
    ap.add_argument("--engine", choices=["auto", "httpx", "curl"], default="auto", help="Chọn engine crawler (mặc định: auto)")
    return ap


if __name__ == "__main__":
    parser = build_arg_parser()
    raise SystemExit(main(parser.parse_args()))
