"""High-efficiency No-Browser Trip.com crawler.

Uses pure HTTP with Chrome 124 TLS impersonation (via curl_cffi), Next.js SSR
stream parsing, and semantic content validation.

Usage:
  # Test a single hotel instantly
  python src/crawl_fast.py --hotel-id 104981087

  # Crawl hotels from latest overview file (Subcritical Direct IP: 2 workers)
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
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import raw_store
from crawl_detail import detail_url, market_raw_dir, save_manifest, targets_from_db, targets_from_file
from engine.http_client_v2 import FastHttpClient
from ssr_extractor import parse_hotel_html


DETAIL_DIR = config.OUTPUT_DIR / "details"
RAW_DIR = DETAIL_DIR / "raw"


def _load_cached_fast(
    hotel_id: str,
    locale: str,
    currency: str,
    allow_legacy: bool = True,
) -> dict | None:
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
        if normalized and normalized.get("success"):
            return normalized
    except Exception:
        pass
    return None


async def crawl_one_fast(
    client: FastHttpClient,
    target: dict,
    checkin: str,
    checkout: str,
    locale: str,
    currency: str,
    include_rooms: bool = False,
) -> dict:
    hotel_id = str(target["trip_hotel_id"])
    url = detail_url(target, checkin, checkout, locale, currency)
    raw_path = market_raw_dir(locale, currency) / f"{hotel_id}.json"

    t0 = time.monotonic()
    html_text, err = await client.get_hotel_page(url, hotel_id=hotel_id)
    elapsed = time.monotonic() - t0

    if err or not html_text:
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
            "error": err or "Empty response",
            "images": [],
            "amenities": [],
            "rooms": [],
            "fetch_time_sec": round(elapsed, 3),
        }
        raw_store.write(raw_path, {"target": target, "normalized": result, "responses": []})
        return result

    try:
        parsed = parse_hotel_html(
            html_text=html_text,
            hotel_id=hotel_id,
            url=url,
            currency=currency,
            locale=locale,
        )
        normalized = parsed["normalized"]
        packets = parsed["packets"]

        rooms = normalized.get("rooms") or []
        normalized.update({
            "locale": locale,
            "currency": currency,
            "check_in": checkin,
            "check_out": checkout,
            "crawled_at": datetime.now().isoformat(timespec="seconds"),
            "rooms_missing": not bool(rooms),
            "success": bool(normalized.get("name") or normalized.get("images") or normalized.get("amenities")),
            "fetch_time_sec": round(elapsed, 3),
        })
        normalized["name"] = normalized.get("name") or target.get("name")
        normalized["address"] = normalized.get("address") or target.get("address")

        raw_data = {
            "target": target,
            "url": url,
            "normalized": normalized,
            "responses": packets,
        }
        raw_store.write(raw_path, raw_data)
        return normalized

    except Exception as exc:
        err_msg = f"ParseError: {type(exc).__name__}: {exc}"
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
            "error": err_msg,
            "images": [],
            "amenities": [],
            "rooms": [],
            "fetch_time_sec": round(elapsed, 3),
        }
        raw_store.write(raw_path, {"target": target, "normalized": result, "responses": []})
        return result


async def main(args: argparse.Namespace) -> None:
    locale = args.locale or config.LOCALE
    currency = (args.currency or config.CURRENCY).upper()
    proxy = (args.proxy or os.getenv("PROXY_URL") or "").strip() or None

    # Determine concurrency
    if args.concurrency:
        concurrency = max(1, args.concurrency)
    elif proxy:
        concurrency = 15  # Default proxy concurrency
    else:
        concurrency = 2   # Safe Subcritical concurrency for Direct IP

    # Target loading
    if args.hotel_id:
        targets = [{"trip_hotel_id": str(args.hotel_id), "name": None, "url": None, "address": None}]
        source = f"single_hotel_{args.hotel_id}"
    elif args.from_db:
        targets, source = targets_from_db(locale, missing_only=args.missing_only)
    else:
        targets, source = targets_from_file(args.file)

    if args.start_after:
        targets = [t for t in targets if int(t["trip_hotel_id"]) > args.start_after]
    if args.limit:
        targets = targets[:args.limit]

    if not targets:
        print("Không có khách sạn nào cần cào.")
        return

    tomorrow = datetime.now() + timedelta(days=1)
    checkin = args.checkin or tomorrow.strftime("%Y-%m-%d")
    checkout = args.checkout or (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    market_tag = f"{locale}_{currency}".replace("-", "")
    out = config.DATA_DIR / f"hotel_details_{market_tag}_{stamp}.json"

    delay_min = args.delay_min if args.delay_min is not None else config.MIN_DELAY
    delay_max = args.delay_max if args.delay_max is not None else config.MAX_DELAY

    print(
        f"=== FAST HTTP CRAWLER (No-Browser) ===\n"
        f"Mục tiêu: {len(targets)} khách sạn | Thị trường: {locale}/{currency}\n"
        f"Chế độ mạng: {'ROTATING PROXY' if proxy else 'DIRECT IP (Subcritical Safe)'}\n"
        f"Concurrency: {concurrency} workers | Delay: {delay_min}s - {delay_max}s"
    )

    detail_slots: list[dict | None] = [None] * len(targets)
    pending: list[tuple[int, dict]] = []

    for index, target in enumerate(targets, start=1):
        if not args.no_resume:
            cached = _load_cached_fast(str(target["trip_hotel_id"]), locale, currency)
            if cached:
                detail_slots[index - 1] = cached
                continue
        pending.append((index, target))

    print(f"Cache: {len(targets) - len(pending)} đã có sẵn, {len(pending)} cần cào mới.\n")

    if not pending:
        details = [v for v in detail_slots if v is not None]
        save_manifest(out, source, details, True, locale, currency)
        print(f"Toàn bộ đã có trong cache -> {out}")
        return

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

            row = await crawl_one_fast(
                client=client,
                target=target,
                checkin=checkin,
                checkout=checkout,
                locale=locale,
                currency=currency,
                include_rooms=args.include_rooms,
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

            print(
                f"[{index}/{len(targets)}] {target['trip_hotel_id']} {status} | "
                f"ảnh={img_c}, tiện ích={amen_c}, phòng={room_c} ({sec}s)"
            )

            if crawled_count % config.CHECKPOINT_EVERY == 0:
                ckpt = [v for v in detail_slots if v is not None]
                save_manifest(out, source, ckpt, False, locale, currency)

            # Auto backoff / stop if high consecutive errors on Direct IP
            if not proxy and consecutive_errors >= args.max_consecutive_errors:
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

    if args.apply_db:
        print(f"\nĐang nạp vào cơ sở dữ liệu PostgreSQL...")
        subprocess.run([sys.executable, "src/db/detail_loader.py", str(out)], check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl hotel details via fast No-Browser HTTP")
    parser.add_argument("--file", help="Đường dẫn file overview JSON")
    parser.add_argument("--hotel-id", help="Cào thử riêng 1 khách sạn theo ID")
    parser.add_argument("--city-id", type=int, help="City ID")
    parser.add_argument("--limit", type=int, help="Giới hạn số khách sạn cần cào")
    parser.add_argument("--start-after", type=int, help="Bỏ qua các ID nhỏ hơn")
    parser.add_argument("--concurrency", type=int, help="Số worker đồng thời (Mặc định: 2 cho Direct IP, 15 cho Proxy)")
    parser.add_argument("--delay-min", type=float, help="Độ trễ tối thiểu giữa các request (giây, mặc định từ .env)")
    parser.add_argument("--delay-max", type=float, help="Độ trễ tối đa giữa các request (giây, mặc định từ .env)")
    parser.add_argument("--proxy", help="URL template proxy (ví dụ http://user-session-{session_id}:pass@gate:port)")
    parser.add_argument("--from-db", action="store_true", help="Lấy danh sách từ DB hotels")
    parser.add_argument("--missing-only", action="store_true", help="Chỉ cào khách sạn thiếu dữ liệu trong DB")
    parser.add_argument("--no-resume", action="store_true", help="Cào lại từ đầu, bỏ qua cache raw")
    parser.add_argument("--apply-db", action="store_true", help="Tự động nạp vào DB sau khi cào xong")
    parser.add_argument("--include-rooms", action="store_true", help="Thu thập thêm phòng nếu có")
    parser.add_argument("--max-consecutive-errors", type=int, default=5, help="Số lỗi liên tiếp tối đa trước khi dừng")
    parser.add_argument("--locale", default=config.LOCALE)
    parser.add_argument("--currency", default=config.CURRENCY)
    parser.add_argument("--checkin", help="YYYY-MM-DD")
    parser.add_argument("--checkout", help="YYYY-MM-DD")

    asyncio.run(main(parser.parse_args()))
