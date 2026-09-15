"""Dò dải giá trị của 3 trục chia đã xác nhận có tác dụng (16, 23, 8).

Vì sao cần: probe_filters.py cho thấy server CHỈ nghe theo type 16 (hạng
sao), type 23 và type 8 (khu vực); còn 17 (sắp xếp), 80 (giá), 15 đều bị
bỏ qua — các con số 6546-7180 chỉ là dao động quanh tổng gốc ~6900.

Script này làm 3 việc:
  1. Đo tổng gốc 3 lần → biết biên độ dao động tự nhiên, khỏi nhầm
     dao động thành "bộ lọc có tác dụng".
  2. Liệt kê 16|0..5 và 23|0..5 → xem tổng các nhánh có bằng tổng gốc
     không. Bằng ≈ chia trọn vẹn, không sót khách sạn nào.
  3. Thử ghép 2 trục (16|x + 23|y) cho nhánh lớn nhất → xem có cắt nhỏ
     được xuống dưới 3000 không.

    python src/probe_partition.py --city-id 301

Khoảng 25 request, chừng 1 phút.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

import config
from crawl_api import PAGE_FETCH_JS, HotelCollector, build_list_url

CAP = 3000          # ngưỡng chặn mềm quan sát được của Trip.com


def f(ftype: str, value: int) -> dict:
    return {"type": ftype, "value": str(value), "filterId": f"{ftype}|{value}"}


async def probe(page, tpl: dict, base: list, extras: list[dict]) -> int | None:
    """Gửi 1 request với danh sách bộ lọc thêm vào → tổng KS server báo."""
    body = json.loads(tpl["post_data"])
    types = {e["type"] for e in extras}
    body["filters"] = [dict(x) for x in base if x.get("type") not in types] + extras
    body["paging"] = {**body.get("paging", {}), "pageIndex": 1, "pageSize": 1}
    body["hotelIdFilter"] = {"hotelAldyShown": []}
    res = await page.evaluate(
        PAGE_FETCH_JS,
        {"url": tpl["url"], "headers": tpl["headers"],
         "body": json.dumps(body, ensure_ascii=False)},
    )
    if res["status"] != 200:
        return None
    try:
        payload = json.loads(res["text"])
    except Exception:
        return None
    return ((payload.get("data") or {}).get("hotelListAddtionInfo") or {}).get("hotelTotalCount")


async def main(args: argparse.Namespace) -> None:
    match = [c for c in config.VN_CITIES if c["id"] == args.city_id]
    if not match:
        raise SystemExit(f"Không có cityId={args.city_id} trong config.VN_CITIES.")
    city = match[0]

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=config.HEADLESS, locale=config.LOCALE,
            timezone_id=config.TIMEZONE, viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        col = HotelCollector(city["name"])
        page.on("response", lambda r: asyncio.create_task(col.on_response(r)))

        d1 = datetime.now() + timedelta(days=1)
        url = build_list_url(city, d1.strftime("%Y-%m-%d"),
                             (d1 + timedelta(days=1)).strftime("%Y-%m-%d"))
        print(f"=== Dò trục chia cho {city['name']} (cityId={city['id']}) ===")
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(4000)
        for _ in range(8):
            if col.template:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(config.SCROLL_PAUSE_MS)
        if not col.template:
            raise SystemExit("Không bắt được mẫu request fetchHotelList — chạy lại giúp em.")

        tpl = col.template
        base = json.loads(tpl["post_data"]).get("filters", [])
        report: dict = {"city": city}

        # --- 1. biên độ dao động tự nhiên ---
        baselines = []
        for _ in range(3):
            baselines.append(await probe(page, tpl, base, []))
            await asyncio.sleep(0.6)
        lo, hi = min(baselines), max(baselines)
        avg = sum(baselines) // len(baselines)
        print(f"\nTổng gốc đo 3 lần: {baselines}  → dao động {lo}-{hi} (±{hi - lo})")
        print(f"Bất kỳ con số nào nằm trong dải này = bộ lọc BỊ BỎ QUA.\n")
        report["baselines"] = baselines

        # --- 2. liệt kê từng trục ---
        axes = {}
        for ftype, label in (("16", "hạng sao"), ("23", "nhóm 23")):
            print(f"--- trục {ftype} ({label}) ---")
            vals = {}
            for v in range(0, 6):
                t = await probe(page, tpl, base, [f(ftype, v)])
                inside = t is not None and lo <= t <= hi
                note = "  (nằm trong dải gốc → bỏ qua)" if inside else ""
                over = "  ⚠ vẫn trên 3000" if t and t > CAP and not inside else ""
                print(f"  {ftype}|{v}: {str(t):>6}{note}{over}")
                vals[v] = t
                await asyncio.sleep(0.6)
            real = {k: v for k, v in vals.items() if v and not (lo <= v <= hi)}
            s = sum(real.values())
            print(f"  → tổng các nhánh có tác dụng: {s} so với gốc ~{avg} "
                  f"({s * 100 // avg if avg else 0}%)\n")
            axes[ftype] = vals

        report["axes"] = axes

        # --- 3. ghép 2 trục cho nhánh lớn nhất ---
        s16 = {k: v for k, v in axes["16"].items() if v and not (lo <= v <= hi)}
        if s16:
            big = max(s16, key=lambda k: s16[k])
            print(f"--- ghép 16|{big} ({s16[big]} KS) với từng nhánh 23 ---")
            combo = {}
            for v in range(0, 6):
                t = await probe(page, tpl, base, [f("16", big), f("23", v)])
                flag = "  ⚠ vẫn trên 3000" if t and t > CAP else ""
                print(f"  16|{big} + 23|{v}: {str(t):>6}{flag}")
                combo[v] = t
                await asyncio.sleep(0.6)
            tot = sum(x for x in combo.values() if x)
            print(f"  → tổng: {tot} so với {s16[big]} của riêng 16|{big}")
            report["combo"] = {"star": big, "values": combo}

        out = config.RECON_DIR / f"partition_probe_{city['id']}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nĐã lưu {out} — gửi em file này hoặc chụp màn hình.")
        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-id", type=int, default=301)
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args))
