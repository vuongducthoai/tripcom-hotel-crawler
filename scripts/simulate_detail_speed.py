"""Script mô phỏng (Simulation Benchmark) đo đạc A/B giữa crawler hiện tại (Baseline)
và crawler tối ưu (Solution 1: Chặn ảnh/font/media + giảm sleep, Solution 2: Concurrency).

Script này hoàn toàn độc lập và không ghi đè dữ liệu production.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src import config
from src.crawl_detail import (
    _capture_hotel_policy_text,
    _capture_response,
    _has_detail,
    _spider_error_code,
    detail_url,
)
from src.detail_extract import extract_detail


async def crawl_one_baseline(
    ctx, target: dict, checkin: str, checkout: str, locale: str, currency: str
) -> dict:
    """Mô phỏng chính xác logic hiện tại của crawl_detail.py (không tối ưu)."""
    start_t = time.perf_counter()
    hotel_id = target["trip_hotel_id"]
    url = detail_url(target, checkin, checkout, locale, currency)
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
        # Chờ tĩnh 3.5s
        await page.wait_for_timeout(3500)
        # Cuộn 3 lần, mỗi lần chờ 900ms
        for ratio in (0.35, 0.70, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
            await page.wait_for_timeout(900)

        for script in await page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                packets.append({
                    "url": "embedded:json-ld", "method": "EMBEDDED", "status": 200,
                    "response": json.loads(script),
                })
            except Exception:
                pass

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

        policy_text = await _capture_hotel_policy_text(page, locale)
        if policy_text:
            packets.append({
                "url": "embedded:hotel-policies", "method": "EMBEDDED", "status": 200,
                "response": {"text": policy_text},
            })

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

        normalized = extract_detail(packets, hotel_id, url, currency, locale)
        duration = time.perf_counter() - start_t
        normalized["_duration"] = duration
        normalized["_packets_count"] = len(packets)
        return normalized
    finally:
        await page.close()


async def crawl_one_optimized(
    ctx, target: dict, checkin: str, checkout: str, locale: str, currency: str
) -> dict:
    """Mô phỏng phiên bản tối ưu (Solution 1: Chặn ảnh/media/font + Giảm sleep chết)."""
    start_t = time.perf_counter()
    hotel_id = target["trip_hotel_id"]
    url = detail_url(target, checkin, checkout, locale, currency)
    page = await ctx.new_page()

    # Solution 1: Chặn tải hình ảnh, video và font chữ
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_(),
    )

    packets: list[dict[str, Any]] = []
    tasks: set[asyncio.Task] = set()

    def on_response(resp) -> None:
        task = asyncio.create_task(_capture_response(resp, packets))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        
        # Cuộn trang nhanh để kích hoạt lazy load
        for ratio in (0.35, 0.70, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
            await page.wait_for_timeout(300)

        # SMART EVENT WAIT: Chờ đúng lúc gói tin phòng và album về (thay vì sleep mù)
        # Giúp không bao giờ bị đóng tab sớm khi mạng chậm, đồng thời kết thúc ngay khi có data
        start_wait = time.perf_counter()
        while time.perf_counter() - start_wait < 8.0:
            urls = [p.get("url", "") for p in packets]
            has_rooms = any("getHotelRoomListOversea" in u for u in urls)
            has_album = any("ctgethotelalbum" in u for u in urls)
            if has_rooms and has_album:
                break
            await asyncio.sleep(0.3)

        for script in await page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                packets.append({
                    "url": "embedded:json-ld", "method": "EMBEDDED", "status": 200,
                    "response": json.loads(script),
                })
            except Exception:
                pass

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

        policy_text = await _capture_hotel_policy_text(page, locale)
        if policy_text:
            packets.append({
                "url": "embedded:hotel-policies", "method": "EMBEDDED", "status": 200,
                "response": {"text": policy_text},
            })

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

        normalized = extract_detail(packets, hotel_id, url, currency, locale)
        duration = time.perf_counter() - start_t
        normalized["_duration"] = duration
        normalized["_packets_count"] = len(packets)
        return normalized
    finally:
        await page.close()


async def run_simulation(sample_size: int = 3, concurrency: int = 3, skip_baseline: bool = False) -> None:
    locale = "vi-VN"
    currency = "VND"
    data_file = config.DATA_DIR / "api_hotels_286_viVN_VND_20260917_091848.json"
    if not data_file.exists():
        # Fallback to any api_hotels_*.json file
        files = sorted(config.DATA_DIR.glob("api_hotels_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            raise SystemExit("Không tìm thấy file api_hotels_*.json để lấy mẫu.")
        data_file = files[0]

    payload = json.loads(data_file.read_text(encoding="utf-8"))
    targets = payload.get("hotels", [])[:sample_size]
    if not targets:
        raise SystemExit("Danh sách hotels rỗng.")

    tomorrow = datetime.now() + timedelta(days=1)
    checkin = tomorrow.strftime("%Y-%m-%d")
    checkout = (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d")

    print("=" * 70)
    print(f"BẮT ĐẦU CHẠY MÔ PHỎNG (SỐ MẪU: {sample_size}, CONCURRENCY: {concurrency})")
    print(f"Mẫu kiểm thử: {', '.join(t['name'] + ' (ID ' + t['trip_hotel_id'] + ')' for t in targets)}")
    print("=" * 70)

    async with async_playwright() as p:
        profile_path = config.profile_dir(locale, currency)
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=config.HEADLESS,
            locale=locale,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )

        baseline_results: list[dict] = []
        baseline_total_time = 0.0

        if not skip_baseline:
            # -------------------------------------------------------------
            # RUN A: BASELINE (Tuần tự 1 tab, tải full ảnh/CSS, sleep cứng)
            # -------------------------------------------------------------
            print("\n▶ [TEST RUN A: BASELINE] Đang chạy (1 tab tuần tự, tải ảnh, sleep gốc)...")
            t0 = time.perf_counter()
            for idx, target in enumerate(targets, 1):
                print(f"  · [{idx}/{sample_size}] Crawling {target['trip_hotel_id']}...")
                res = await crawl_one_baseline(ctx, target, checkin, checkout, locale, currency)
                baseline_results.append(res)
                print(f"    -> Xong trong {res['_duration']:.2f}s | Ảnh: {len(res.get('images') or [])}, "
                      f"Phòng: {len(res.get('rooms') or [])}, Tiện ích: {len(res.get('amenities') or [])}, "
                      f"Chính sách: {len(res.get('policies') or [])}")
                await asyncio.sleep(2.0)
            baseline_total_time = time.perf_counter() - t0
            print(f"✔ [TEST RUN A HOÀN TẤT] Tổng thời gian: {baseline_total_time:.2f}s "
                  f"(Trung bình: {baseline_total_time / sample_size:.2f}s / KS)")
            await asyncio.sleep(3.0)

        # -------------------------------------------------------------
        # RUN B: OPTIMIZED (Solution 1: Chặn ảnh/font + Solution 2: N tabs song song)
        # -------------------------------------------------------------
        actual_concurrency = min(concurrency, sample_size)
        print(f"\n▶ [TEST RUN B: OPTIMIZED] Đang chạy (Chặn ảnh/font + {actual_concurrency} tabs song song)...")
        sem = asyncio.Semaphore(actual_concurrency)

        async def worker(idx: int, target: dict) -> dict:
            async with sem:
                print(f"  · [Tab {idx}] Bắt đầu {target['trip_hotel_id']}...")
                # Delay ngẫu nhiên nhẹ giữa các tab để không trùng miligiây
                await asyncio.sleep(random.uniform(0.1, 0.4))
                res = await crawl_one_optimized(ctx, target, checkin, checkout, locale, currency)
                print(f"  · [Tab {idx}] Xong {target['trip_hotel_id']} trong {res['_duration']:.2f}s | "
                      f"Ảnh: {len(res.get('images') or [])}, Phòng: {len(res.get('rooms') or [])}")
                return res

        t1 = time.perf_counter()
        optimized_results = await asyncio.gather(
            *(worker(i, t) for i, t in enumerate(targets, 1))
        )
        optimized_total_time = time.perf_counter() - t1
        print(f"✔ [TEST RUN B HOÀN TẤT] Tổng thời gian: {optimized_total_time:.2f}s "
              f"(Trung bình thông lượng: {optimized_total_time / sample_size:.2f}s / KS)")

        await ctx.close()

    # -------------------------------------------------------------
    # BÁO CÁO ĐỐI SOÁT & KIỂM TRA TOÀN VẸN DỮ LIỆU (DATA INTEGRITY)
    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("BÁO CÁO SO SÁNH & ĐỐI SOÁT TOÀN VẸN DỮ LIỆU (SIMULATION REPORT)")
    print("=" * 70)

    if baseline_results:
        print("\n1. HIỆU NĂNG TỐC ĐỘ (SPEED BENCHMARK):")
        speedup = baseline_total_time / max(optimized_total_time, 0.001)
        print(f"   • Thời gian Baseline : {baseline_total_time:.2f} giây ({baseline_total_time / sample_size:.2f}s / KS)")
        print(f"   • Thời gian Optimized: {optimized_total_time:.2f} giây ({optimized_total_time / sample_size:.2f}s / KS)")
        print(f"   • TỐC ĐỘ TĂNG TRƯỞNG : NHANH HƠN {speedup:.2f} LẦN ({speedup*100 - 100:.1f}%)")
        est_hours_base = (baseline_total_time / sample_size * 2444) / 3600
        est_hours_opt = (optimized_total_time / sample_size * 2444) / 3600
        print(f"   • Ước tính cho 2.444 KS: Baseline mất ~{est_hours_base:.1f} tiếng | Optimized chỉ mất ~{est_hours_opt:.1f} tiếng (~{est_hours_opt * 60:.0f} phút)")

        print("\n2. ĐỐI SOÁT TOÀN VẸN DỮ LIỆU (DATA INTEGRITY CHECK):")
        perfect_match = True
        for i, target in enumerate(targets):
            base = baseline_results[i]
            opt = optimized_results[i]
            hid = target["trip_hotel_id"]
            hname = target["name"]

            base_imgs = len(base.get("images") or [])
            opt_imgs = len(opt.get("images") or [])

            base_rooms = len(base.get("rooms") or [])
            opt_rooms = len(opt.get("rooms") or [])

            base_amenities = len(base.get("amenities") or [])
            opt_amenities = len(opt.get("amenities") or [])

            base_policies = len(base.get("policies") or [])
            opt_policies = len(opt.get("policies") or [])

            match_imgs = (base_imgs == opt_imgs)
            match_rooms = (base_rooms == opt_rooms)
            match_amenities = (base_amenities == opt_amenities)
            match_policies = (base_policies == opt_policies)

            is_match = match_imgs and match_rooms and match_amenities and match_policies
            if not is_match:
                perfect_match = False

            status = "KHỚP 100% (PERFECT)" if is_match else "CHÊNH LỆCH"
            print(f"\n   Khách sạn [{hid}] {hname} -> {status}:")
            print(f"     - Số lượng ảnh      : Baseline = {base_imgs:<4} | Optimized = {opt_imgs:<4} | {'OK' if match_imgs else 'FAIL'}")
            print(f"     - Số loại phòng     : Baseline = {base_rooms:<4} | Optimized = {opt_rooms:<4} | {'OK' if match_rooms else 'FAIL'}")
            print(f"     - Số tiện ích       : Baseline = {base_amenities:<4} | Optimized = {opt_amenities:<4} | {'OK' if match_amenities else 'FAIL'}")
            print(f"     - Số chính sách     : Baseline = {base_policies:<4} | Optimized = {opt_policies:<4} | {'OK' if match_policies else 'FAIL'}")

        print("\n3. KẾT LUẬN KIỂM ĐỊNH:")
        if perfect_match:
            print("   >>> CHỨNG NHẬN: Dữ liệu giữa bản Optimized và Baseline KHỚP 100%!")
        else:
            print("   >>> CẢNH BÁO: Có sự chênh lệch nhỏ giữa 2 phiên bản, cần xem chi tiết ở trên.")
    else:
        print("\n1. HIỆU NĂNG TỐC ĐỘ:")
        print(f"   • Thời gian Optimized: {optimized_total_time:.2f} giây ({optimized_total_time / sample_size:.2f}s / KS)")
        est_hours_opt = (optimized_total_time / sample_size * 2444) / 3600
        print(f"   • Ước tính cho 2.444 KS: ~{est_hours_opt:.1f} tiếng (~{est_hours_opt * 60:.0f} phút)")
        print("\n2. KẾT QUẢ THU THẬP TỪNG KHÁCH SẠN:")
        for i, target in enumerate(targets):
            opt = optimized_results[i]
            hid = target["trip_hotel_id"]
            hname = target["name"]
            print(f"   · [{hid}] {hname}: Ảnh={len(opt.get('images') or [])}, Phòng={len(opt.get('rooms') or [])}, Tiện ích={len(opt.get('amenities') or [])}, Chính sách={len(opt.get('policies') or [])}")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=3, help="Số lượng KS chạy thử nghiệm")
    parser.add_argument("--concurrency", type=int, default=3, help="Số tabs song song")
    parser.add_argument("--skip-baseline", action="store_true", help="Bỏ qua baseline, chỉ test optimized")
    args = parser.parse_args()
    asyncio.run(run_simulation(args.sample_size, args.concurrency, args.skip_baseline))
