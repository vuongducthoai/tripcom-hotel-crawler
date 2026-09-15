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

DETAIL_DIR = config.OUTPUT_DIR / "details"
RAW_DIR = DETAIL_DIR / "raw"
DETAIL_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

MAX_RESPONSE_BYTES = 6_000_000
MAX_RESPONSES = 50


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
                "url": hotel.get("url"),
            })
    return targets, path.name


def targets_from_db() -> tuple[list[dict], str]:
    import psycopg2

    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT trip_hotel_id, name, url FROM hotels "
            "WHERE trip_hotel_id IS NOT NULL ORDER BY id"
        )
        rows = cur.fetchall()
    return [
        {"trip_hotel_id": str(hotel_id), "name": name, "url": url}
        for hotel_id, name, url in rows
    ], "postgres"


def detail_url(target: dict, checkin: str, checkout: str) -> str:
    hotel_id = target["trip_hotel_id"]
    raw = target.get("url") or f"https://vn.trip.com/hotels/detail/?hotelId={hotel_id}"
    parsed = urllib.parse.urlsplit(raw)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({
        "hotelId": hotel_id,
        "checkIn": checkin,
        "checkOut": checkout,
        "adult": "2",
        "children": "0",
        "crn": "1",
        "curr": "VND",
        "locale": "vi-VN",
    })
    return urllib.parse.urlunsplit((
        parsed.scheme or "https",
        parsed.netloc or "vn.trip.com",
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


async def crawl_one(ctx, target: dict, checkin: str, checkout: str) -> dict:
    hotel_id = target["trip_hotel_id"]
    url = detail_url(target, checkin, checkout)
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

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        normalized = extract_detail(packets, hotel_id, url)
        normalized.update({
            "name": target.get("name"),
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "success": bool(packets),
            "error": None if packets else "không bắt được JSON detail",
        })
        raw = {"target": target, "url": url, "normalized": normalized, "responses": packets}
        (RAW_DIR / f"{hotel_id}.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return normalized
    except Exception as exc:
        result = {
            "trip_hotel_id": hotel_id,
            "name": target.get("name"),
            "url": url,
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "images": [], "amenities": [], "rooms": [],
        }
        (RAW_DIR / f"{hotel_id}.json").write_text(
            json.dumps({"target": target, "normalized": result, "responses": packets},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    finally:
        await page.close()


def _load_cached(hotel_id: str) -> dict | None:
    path = RAW_DIR / f"{hotel_id}.json"
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
            )
            value.update(fresh)
            dump["normalized"] = value
            path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            return None
    return value if value and value.get("success") else None


def save_manifest(path: Path, source: str, details: list[dict], complete: bool) -> None:
    path.write_text(json.dumps({
        "source_overview": source,
        "crawled_at": datetime.now().isoformat(timespec="seconds"),
        "complete": complete,
        "count": len(details),
        "success_count": sum(1 for row in details if row.get("success")),
        "details": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(args: argparse.Namespace) -> None:
    targets, source = targets_from_db() if args.from_db else targets_from_file(args.file)
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
    out = config.DATA_DIR / f"hotel_details_{stamp}.json"
    details: list[dict] = []

    print(f"Detail: {len(targets)} hotel | {checkin} → {checkout}")
    print("Không chạy song song với crawl_api.py (dùng chung browser_profile).")

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=str(config.PROFILE_DIR),
                headless=config.HEADLESS,
                locale=config.LOCALE,
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
            cached = _load_cached(target["trip_hotel_id"]) if not args.no_resume else None
            if cached:
                details.append(cached)
                print(f"[{index}/{len(targets)}] cache {target['trip_hotel_id']}")
                continue
            row = await crawl_one(ctx, target, checkin, checkout)
            details.append(row)
            print(
                f"[{index}/{len(targets)}] {target['trip_hotel_id']} "
                f"{'OK' if row.get('success') else 'LỖI'} | "
                f"ảnh={len(row.get('images') or [])}, "
                f"tiện ích={len(row.get('amenities') or [])}, "
                f"phòng={len(row.get('rooms') or [])}"
            )
            if index % config.CHECKPOINT_EVERY == 0:
                save_manifest(out, source, details, False)
                print(f"  checkpoint → {out}")
            await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))

        await ctx.close()

    success = sum(1 for row in details if row.get("success"))
    save_manifest(out, source, details, success == len(details))
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
    ap.add_argument("--no-resume", action="store_true", help="crawl lại cả hotel đã có raw thành công")
    parsed = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(parsed))
