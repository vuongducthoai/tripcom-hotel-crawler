"""Dò xem Trip.com chấp nhận những bộ lọc nào — để chia nhỏ truy vấn.

Vì sao cần: server chặn mềm ở ~3000 kết quả mỗi lần tìm (TP.HCM có 6545 KS
nhưng crawl thẳng chỉ ra 3047). Muốn lấy đủ phải chia truy vấn thành nhiều
mảnh nhỏ, mỗi mảnh dưới 3000. Nhưng phải biết CHẮC bộ lọc nào server nghe
theo, không đoán mò.

Cách dò: gửi mỗi ứng viên 1 request pageSize=1, đọc `hotelTotalCount`.
  - Tổng ĐỔI so với gốc  → server có áp bộ lọc → chia được theo nó.
  - Tổng GIỮ NGUYÊN      → server bỏ qua → vô dụng.

    python src/probe_filters.py --city-id 301

Chạy khoảng 1 phút, ~25 request. Kết quả in ra bảng + lưu
output/recon/filter_probe_<cityId>.json
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

# Mỗi ứng viên: (nhóm, nhãn, filter thêm/thay vào)
# type 17 = sắp xếp, 80 = giá, 16/23/15 = hạng sao, 8/13 = khu vực
CANDIDATES: list[tuple[str, str, dict]] = [
    # --- sắp xếp: nếu đổi được thì mỗi kiểu sắp xếp cho 1 "cửa sổ 3000" khác nhau
    *[("sắp xếp", f"17|{v}", {"type": "17", "value": str(v), "filterId": f"17|{v}"})
      for v in (1, 2, 3, 4, 5)],
    # --- giá: dạng 80|min|max (đoán theo VND)
    ("giá", "80|0|500000", {"type": "80", "value": "0", "filterId": "80|0|500000"}),
    ("giá", "80|500000|1000000", {"type": "80", "value": "0", "filterId": "80|500000|1000000"}),
    ("giá", "80|1000000|99000000", {"type": "80", "value": "0", "filterId": "80|1000000|99000000"}),
    # --- giá: dạng chỉ số bậc 80|bậc|đơnvị
    *[("giá-bậc", f"80|{i}|1", {"type": "80", "value": str(i), "filterId": f"80|{i}|1"})
      for i in (1, 2, 3)],
    # --- hạng sao, thử vài mã type hay gặp
    *[("sao", f"{t}|{s}", {"type": t, "value": str(s), "filterId": f"{t}|{s}"})
      for t in ("16", "23", "15") for s in (3, 5)],
    # --- khu vực: mã server tự trả về, chắc chắn hợp lệ → dùng làm mốc đối chiếu
    ("khu vực", "8|107182738", {"type": "8", "value": "107182738|10.773954024|106.695477963|1",
                                 "filterId": "8|107182738", "subType": "2"}),
]


async def probe_one(page, tpl: dict, base_filters: list, extra: dict | None) -> tuple:
    """Gửi 1 request với bộ lọc thêm vào → (tổng KS, số entry trả về)."""
    body = json.loads(tpl["post_data"])
    filters = [dict(f) for f in base_filters]
    if extra:
        # thay thế nếu đã có cùng type, không thì thêm mới
        filters = [f for f in filters if f.get("type") != extra.get("type")]
        filters.append(extra)
    body["filters"] = filters
    body["paging"] = {**body.get("paging", {}), "pageIndex": 1, "pageSize": 1}
    body["hotelIdFilter"] = {"hotelAldyShown": []}

    res = await page.evaluate(
        PAGE_FETCH_JS,
        {"url": tpl["url"], "headers": tpl["headers"],
         "body": json.dumps(body, ensure_ascii=False)},
    )
    if res["status"] != 200:
        return None, f"HTTP {res['status']}"
    try:
        payload = json.loads(res["text"])
    except Exception:
        return None, "không phải JSON"
    data = payload.get("data") or {}
    addi = data.get("hotelListAddtionInfo") or {}
    return addi.get("hotelTotalCount"), len(data.get("hotelList") or [])


async def main(args: argparse.Namespace) -> None:
    cities = [c for c in config.VN_CITIES if c["id"] == args.city_id]
    if not cities:
        raise SystemExit(f"Không có cityId={args.city_id} trong config.VN_CITIES.")
    city = cities[0]

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=config.HEADLESS,
            locale=config.LOCALE,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        collector = HotelCollector(city["name"])
        page.on("response", lambda r: asyncio.create_task(collector.on_response(r)))

        tomorrow = datetime.now() + timedelta(days=1)
        url = build_list_url(city, tomorrow.strftime("%Y-%m-%d"),
                             (tomorrow + timedelta(days=1)).strftime("%Y-%m-%d"))
        print(f"=== Dò bộ lọc cho {city['name']} (cityId={city['id']}) ===")
        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(4000)

        for _ in range(8):
            if collector.template:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(config.SCROLL_PAUSE_MS)

        if not collector.template:
            raise SystemExit("Không bắt được mẫu request fetchHotelList — thử chạy lại.")

        tpl = collector.template
        base_filters = json.loads(tpl["post_data"]).get("filters", [])

        baseline, n = await probe_one(page, tpl, base_filters, None)
        print(f"\nGốc (không thêm lọc): tổng = {baseline}\n")
        print(f"{'nhóm':<10} {'bộ lọc':<22} {'tổng KS':>10}   kết luận")
        print("-" * 68)

        results = []
        for group, label, extra in CANDIDATES:
            total, n = await probe_one(page, tpl, base_filters, extra)
            if total is None:
                verdict = f"lỗi: {n}"
            elif baseline and total == baseline:
                verdict = "bị bỏ qua"
            elif total == 0:
                verdict = "hợp lệ nhưng rỗng"
            else:
                verdict = "✓ CÓ ÁP DỤNG — chia được"
            print(f"{group:<10} {label:<22} {str(total):>10}   {verdict}")
            results.append({"group": group, "filter": label, "total": total,
                            "verdict": verdict, "extra": extra})
            await asyncio.sleep(0.6)

        out = config.RECON_DIR / f"filter_probe_{city['id']}.json"
        out.write_text(json.dumps(
            {"city": city, "baseline_total": baseline, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nĐã lưu {out}")
        print("Gửi em file này (hoặc chụp bảng trên) để em viết bộ chia phù hợp.")

        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-id", type=int, default=301)
    args = ap.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args))
