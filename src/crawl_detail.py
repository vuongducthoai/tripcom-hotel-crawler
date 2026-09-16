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

from playwright.async_api import async_playwright

import config
from detail_extract import PARSER_VERSION, extract_detail

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DETAIL_DIR = config.OUTPUT_DIR / "details"
RAW_DIR = DETAIL_DIR / "raw"
DETAIL_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

MAX_RESPONSE_BYTES = 6_000_000
MAX_RESPONSES = 50


def _spider_error_code(value: Any) -> str | None:
    if isinstance(value, dict):
        if value.get("htlSpiderActionErrorCode") is not None:
            return str(value["htlSpiderActionErrorCode"])
        for child in value.values():
            found = _spider_error_code(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _spider_error_code(child)
            if found:
                return found
    return None


def _has_detail(value: dict) -> bool:
    return bool(
        value.get("description") or value.get("hotel_type")
        or value.get("images") or value.get("amenities") or value.get("rooms")
    )


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


def targets_from_db(locale: str = "vi-VN") -> tuple[list[dict], str]:
    import psycopg2

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.trip_hotel_id, COALESCE(t.name, h.name), h.url, t.address
            FROM hotels h
            LEFT JOIN hotel_translations t
              ON t.hotel_id=h.id AND t.locale=%s
            WHERE h.trip_hotel_id IS NOT NULL
            ORDER BY h.id
            """,
            (locale,),
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


def market_raw_dir(locale: str, currency: str) -> Path:
    path = RAW_DIR / locale / currency.upper()
    path.mkdir(parents=True, exist_ok=True)
    return path


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
        await page.wait_for_timeout(3500)
        # Kích hoạt lazy-load ảnh, tiện ích và room inventory.
        for ratio in (0.35, 0.70, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
            await page.wait_for_timeout(900)

        # JSON-LD thường chứa mô tả/ảnh ngay cả khi API đổi endpoint.
        for script in await page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                packets.append({"url": "embedded:json-ld", "method": "EMBEDDED", "status": 200,
                                "response": json.loads(script)})
            except Exception:
                pass

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

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        normalized = extract_detail(packets, hotel_id, url, currency)
        spider_code = next(
            (code for packet in packets
             if (code := _spider_error_code(packet.get("response"))) is not None),
            None,
        )
        has_detail = _has_detail(normalized)
        normalized.update({
            "locale": locale,
            "currency": currency,
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "success": has_detail and spider_code is None,
            "error": (
                f"Trip.com anti-crawler code {spider_code}" if spider_code
                else None if has_detail
                else "có JSON response nhưng không trích được dữ liệu detail"
            ),
        })
        normalized["name"] = normalized.get("name") or target.get("name")
        normalized["address"] = normalized.get("address") or target.get("address")
        raw = {"target": target, "url": url, "normalized": normalized, "responses": packets}
        raw_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
        raw_path.write_text(
            json.dumps({"target": target, "normalized": result, "responses": packets},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    finally:
        await page.close()


def _load_cached(hotel_id: str, locale: str, currency: str) -> dict | None:
    path = market_raw_dir(locale, currency) / f"{hotel_id}.json"
    legacy_path = RAW_DIR / f"{hotel_id}.json"
    if (
        not path.exists() and locale == "vi-VN" and currency.upper() == "VND"
        and legacy_path.exists()
    ):
        path = legacy_path
    if not path.exists():
        return None
    try:
        dump = json.loads(path.read_text(encoding="utf-8"))
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
            )
            fresh["name"] = fresh.get("name") or value.get("name") or target.get("name")
            fresh["address"] = (
                fresh.get("address") or value.get("address") or target.get("address")
            )
            value.update(fresh)
            dump["normalized"] = value
            path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return None
    if value:
        value.setdefault("locale", locale)
        value.setdefault("currency", currency)
    has_spider_error = any(
        _spider_error_code(packet.get("response")) is not None
        for packet in (dump.get("responses") or [])
    )
    return value if value and value.get("success") and _has_detail(value) and not has_spider_error else None


def save_manifest(
    path: Path, source: str, details: list[dict], complete: bool,
    locale: str, currency: str,
) -> None:
    path.write_text(json.dumps({
        "source_overview": source,
        "locale": locale,
        "currency": currency,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
        "complete": complete,
        "count": len(details),
        "success_count": sum(1 for row in details if row.get("success")),
        "details": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    locale = args.locale or config.LOCALE
    currency = (args.currency or config.CURRENCY).upper()
    targets, source = targets_from_db(locale) if args.from_db else targets_from_file(args.file)
    if args.start_after:
        targets = [t for t in targets if int(t["trip_hotel_id"]) > args.start_after]
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        raise SystemExit("Không có hotel nào để crawl detail.")

    tomorrow = datetime.now() + timedelta(days=1)
    checkin = args.checkin or tomorrow.strftime("%Y-%m-%d")
    checkout = args.checkout or (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    market_tag = f"{locale}_{currency}".replace("-", "")
    out = config.DATA_DIR / f"hotel_details_{market_tag}_{stamp}.json"
    details: list[dict] = []

    print(f"Detail: {len(targets)} hotel | {locale}/{currency} | {checkin} → {checkout}")
    print("Không chạy song song với crawl_api.py (dùng chung browser_profile).")

    async with async_playwright() as p:
        profile_path = Path(args.profile_dir) if args.profile_dir else config.profile_dir(locale, currency)
        if not profile_path.is_absolute():
            profile_path = config.ROOT / profile_path
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path),
                headless=config.HEADLESS,
                locale=locale,
                timezone_id=config.TIMEZONE,
                viewport=config.VIEWPORT,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            raise SystemExit(
                "Không mở được browser_profile. Hãy dừng crawl_api.py/Chromium trước.\n"
                f"Chi tiết: {exc}"
            ) from exc

        for index, target in enumerate(targets, 1):
            cached = _load_cached(target["trip_hotel_id"], locale, currency) if not args.no_resume else None
            if cached:
                details.append(cached)
                print(f"[{index}/{len(targets)}] cache {target['trip_hotel_id']}")
                continue
            row = await crawl_one(ctx, target, checkin, checkout, locale, currency)
            details.append(row)
            print(
                f"[{index}/{len(targets)}] {target['trip_hotel_id']} "
                f"{'OK' if row.get('success') else 'LỖI'} | "
                f"ảnh={len(row.get('images') or [])}, "
                f"tiện ích={len(row.get('amenities') or [])}, "
                f"phòng={len(row.get('rooms') or [])}"
            )
            if index % config.CHECKPOINT_EVERY == 0:
                save_manifest(out, source, details, False, locale, currency)
                print(f"  checkpoint → {out}")
            await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))

        await ctx.close()

    success = sum(1 for row in details if row.get("success"))
    save_manifest(out, source, details, success == len(details), locale, currency)
    print(f"→ {success}/{len(details)} detail thành công → {out}")
    print(f"Nạp DB: python src/db/detail_loader.py {out.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="file overview api_hotels_*.json; mặc định file mới nhất")
    ap.add_argument("--from-db", action="store_true", help="đọc danh sách hotel từ PostgreSQL")
    ap.add_argument("--limit", type=int, help="chỉ crawl N hotel đầu (nên dùng 1 để kiểm thử)")
    ap.add_argument("--start-after", type=int, help="chỉ lấy trip_hotel_id lớn hơn giá trị này")
    ap.add_argument("--checkin", help="YYYY-MM-DD")
    ap.add_argument("--checkout", help="YYYY-MM-DD")
    ap.add_argument("--locale", default=config.LOCALE, help="Trip.com locale, ví dụ vi-VN hoặc en-US")
    ap.add_argument("--currency", default=config.CURRENCY, help="Mã tiền tệ, ví dụ VND hoặc USD")
    ap.add_argument("--profile-dir", help="profile Chromium tùy chọn; mặc định tách theo market")
    ap.add_argument("--no-resume", action="store_true", help="crawl lại cả hotel đã có raw thành công")
    parsed = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(parsed))
