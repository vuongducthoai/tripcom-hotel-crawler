"""Bước 3 (đường A) — crawl danh sách khách sạn qua API phân trang thật.

Lịch sử điều tra (xem docs/recon.md):
  - `fetchDynamicRefreshList` CHỈ refresh giá cho card đã hiện, không phải
    nguồn danh sách — dò nhầm ban đầu.
  - Trang 1 nằm sẵn trong HTML (SSR, Next.js `self.__next_f.push`).
  - Trang 2 trở đi gọi `POST /restapi/soa2/34951/fetchHotelList` với
    body chứa `paging.pageIndex` + `hotelIdFilter.hotelAldyShown`.
  - Cuộn chỉ kéo được vài chục khách sạn rồi trang tự báo hết (isLastPage).
    Nên KHÔNG cuộn để lấy hết nữa: cuộn 1-2 vòng chỉ để BẮT MẪU request
    (kèm token chống bot do chính trang sinh ra), rồi phát lại request đó
    ngay TRONG trang, tăng dần pageIndex.
  - Trip.com còn CHẶN MỀM ở khoảng 3000 KS mỗi truy vấn — thành phố lớn hơn
    (TP.HCM ~6500-7000) sẽ bị cắt giữa chừng dù gọi đúng API. Xác nhận qua
    probe_filters.py: 2 loại filter "16" và "23" server có áp dụng thật (số
    lượng đổi), còn "17" (sắp xếp), "80" (giá), "15" bị bỏ qua. Hai loại này
    có vẻ là nhãn/tag (nhánh có thể CHỒNG LÊN NHAU, tổng > tổng gốc) chứ
    không phải phân loại tách biệt — không sao, vì kết quả luôn dedupe theo
    trip_hotel_id nên chồng lấn chỉ tốn thêm request, không gây sai lệch.
    → Khi 1 thành phố vượt PARTITION_CAP (config.py), tự động chia truy vấn
    theo "16" rồi "23" (đệ quy), mỗi mảnh dưới ngưỡng mới thật sự crawl.

    python src/crawl_api.py                       # chạy hết VN_CITIES
    python src/crawl_api.py --city-id 301
    python src/crawl_api.py --city "Đà Nẵng"
    python src/crawl_api.py --city-id 301 --max-pages 5    # chạy thử nhanh

Kết quả: output/data/api_hotels_<cityId>_<timestamp>.json
File được ghi lại sau mỗi 20 trang (checkpoint) nên đứt giữa chừng
vẫn còn dữ liệu, không mất trắng. Con số "tổng KS" Trip.com tự báo có dao
động nhẹ giữa các lần gọi (quan sát được ±5-8%) — coi tỉ lệ % cuối log là
ước lượng, không phải con số tuyệt đối.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

from playwright.async_api import async_playwright

import config
from api_extract import dedupe, extract_from_html, find_hotel_lists, is_last_page, parse_hotel

NOISE = re.compile(r"(google|gstatic|doubleclick|facebook|sentry|/log|/track|bee/collect)", re.I)
HOTEL_SERVICE = "/restapi/soa2/34951/"
LIST_ENDPOINT = "fetchHotelList"

# fetch() của trình duyệt tự quản lý các header này, truyền vào sẽ bị bỏ
# qua hoặc báo lỗi — phải lọc ra khỏi mẫu bắt được.
FORBIDDEN_HEADERS = {
    "host", "connection", "content-length", "cookie", "cookie2", "date",
    "accept-encoding", "accept-charset", "origin", "referer", "te", "trailer",
    "transfer-encoding", "upgrade", "via", "expect", "dnt", "keep-alive",
}


def build_list_url(city: dict, checkin: str, checkout: str) -> str:
    """Dựng thẳng URL trang danh sách, khỏi phải gõ ô tìm kiếm + click gợi ý."""
    name = city["name"]
    params = {
        "flexType": "1",
        "cityId": str(city["id"]),
        "provinceId": "0",
        "districtId": "0",
        "countryId": "111",
        "cityName": name,
        "destName": f"{name}, Việt Nam",
        "searchWord": name,
        "searchType": "CT",
        "optionId": str(city["id"]),
        "searchValue": f"19|{city['id']}*19*{city['id']}",
        "checkin": checkin,
        "checkout": checkout,
        "crn": "1",
        "adult": "2",
        "curr": "VND",
        "locale": "vi-VN",
        "old": "1",
    }
    return "https://vn.trip.com/hotels/list?" + urllib.parse.urlencode(
        params, quote_via=urllib.parse.quote
    )


class HotelCollector:
    """Nghe response JSON, thu khách sạn VÀ bắt mẫu request phân trang."""

    def __init__(self, city_name: str) -> None:
        self.city_name = city_name
        self.rows: list[dict] = []
        self.last_page_seen = False
        self.hotel_service_calls = 0
        self.sources: dict[str, int] = {}
        self.template: dict | None = None     # mẫu request fetchHotelList
        self.dump_dir = config.RECON_DIR / "after_search"
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        self._dumped: set[str] = set()

    async def on_response(self, resp) -> None:
        req = resp.request
        if req.resource_type not in ("xhr", "fetch") or NOISE.search(req.url):
            return
        try:
            body = await resp.json()
        except Exception:
            return

        if HOTEL_SERVICE in req.url:
            self.hotel_service_calls += 1
            self._dump_once(req, body)

        # Bắt mẫu request phân trang — chỉ cần 1 lần, có token là dùng lại được.
        if LIST_ENDPOINT in req.url and self.template is None and req.post_data:
            try:
                headers = await req.all_headers()
            except Exception:
                headers = req.headers
            self.template = {
                "url": req.url,
                "headers": {
                    k: v for k, v in headers.items()
                    if not k.startswith(":") and k.lower() not in FORBIDDEN_HEADERS
                },
                "post_data": req.post_data,
            }

        hits = find_hotel_lists(body)
        if hits:
            endpoint = req.url.split("?")[0]
            got = 0
            for _, arr in hits:
                for entry in arr:
                    row = parse_hotel(entry, self.city_name)
                    if row:
                        self.rows.append(row)
                        got += 1
            if got:
                self.sources[endpoint] = self.sources.get(endpoint, 0) + got

        # CHỈ tin isLastPage từ đúng endpoint danh sách. Trước đây tin cả
        # response khác nên dừng oan ở 49 KS trong khi thành phố có 6546.
        if LIST_ENDPOINT in req.url and is_last_page(body):
            self.last_page_seen = True

    def _dump_once(self, req, body) -> None:
        endpoint = req.url.split("?")[0]
        if endpoint in self._dumped:
            return
        self._dumped.add(endpoint)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", endpoint)[-80:]
        (self.dump_dir / f"{name}.json").write_text(
            json.dumps(
                {"url": req.url, "post_data": req.post_data, "response": body},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )


SCROLL_TO_BOTTOM_JS = "window.scrollTo(0, document.body.scrollHeight); document.body.scrollHeight"

# Phát lại request NGAY TRONG trang: cookie, service worker và mọi lớp
# chống bot của trang đều tự áp dụng, giống hệt lúc trang tự gọi.
PAGE_FETCH_JS = """
async ({url, headers, body}) => {
  try {
    const r = await fetch(url, {
      method: 'POST', headers, body,
      credentials: 'include', mode: 'cors',
    });
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: -1, text: String(e)};
  }
}
"""


def _apply_extra(filters: list[dict], extra: list[dict]) -> list[dict]:
    """Thay/thêm filter theo type — dùng cho cả probe lẫn crawl thật."""
    types = {e["type"] for e in extra}
    return [dict(f) for f in filters if f.get("type") not in types] + list(extra)


def _fmt_extra(extra: list[dict] | None) -> str:
    if not extra:
        return "(toàn bộ)"
    return "+".join(f"{e['type']}={e['value']}" for e in extra)


async def probe_count(page, tpl: dict, base_filters: list[dict], extra: list[dict]) -> int | None:
    """1 request pageSize=1 → hotelTotalCount server báo cho tổ hợp filter này."""
    body = json.loads(tpl["post_data"])
    body["filters"] = _apply_extra(base_filters, extra)
    body["paging"] = {**body.get("paging", {}), "pageIndex": 1, "pageSize": 1}
    body["hotelIdFilter"] = {"hotelAldyShown": []}
    res = await page.evaluate(
        PAGE_FETCH_JS,
        {"url": tpl["url"], "headers": tpl["headers"], "body": json.dumps(body, ensure_ascii=False)},
    )
    if res["status"] != 200:
        return None
    try:
        payload = json.loads(res["text"])
    except Exception:
        return None
    return ((payload.get("data") or {}).get("hotelListAddtionInfo") or {}).get("hotelTotalCount")


async def build_partition(
    page, tpl: dict, base_filters: list[dict],
    parent_extra: list[dict], parent_count: int, axis_i: int = 0,
    budget: list[int] | None = None,
) -> list[tuple[list[dict], int]]:
    """Chia đệ quy theo config.PARTITION_AXES tới khi mỗi mảnh <= PARTITION_CAP.

    Trả về list (extra_filters, tổng_ước_lượng) — mỗi phần tử là 1 truy vấn
    sẽ chạy paginate() riêng. Giá trị không thật sự lọc được (server trả về
    ~bằng mảnh cha) tự động bị bỏ qua, khỏi tạo request thừa.
    """
    if budget is None:
        budget = [config.PARTITION_MAX_LEAVES]

    if parent_count is None or parent_count <= config.PARTITION_CAP or axis_i >= len(config.PARTITION_AXES):
        return [(parent_extra, parent_count)]

    ftype = config.PARTITION_AXES[axis_i]
    leaves: list[tuple[list[dict], int]] = []
    for v in config.PARTITION_AXIS_VALUES:
        if budget[0] <= 0:
            print(f"    ⚠ Chạm giới hạn {config.PARTITION_MAX_LEAVES} mảnh — dừng chia thêm, "
                  f"có thể thiếu sót ở '{_fmt_extra(parent_extra)}'.")
            break
        extra = parent_extra + [{"type": ftype, "value": str(v), "filterId": f"{ftype}|{v}"}]
        cnt = await probe_count(page, tpl, base_filters, extra)
        await asyncio.sleep(config.API_MIN_DELAY / 2)
        if not cnt:
            continue
        # Giá trị "không lọc thật" (server bỏ qua) → gần bằng mảnh cha, bỏ.
        if cnt >= parent_count * 0.97:
            continue
        budget[0] -= 1
        leaves.extend(await build_partition(page, tpl, base_filters, extra, cnt, axis_i + 1, budget))

    if not leaves:
        # Trục này không cắt được gì (mọi giá trị đều bị bỏ qua) — đành nhận
        # nguyên mảnh cha, chấp nhận có thể thiếu sót ở phần vượt ngưỡng.
        return [(parent_extra, parent_count)]
    return leaves


def _save(city: dict, rows: list[dict], out: Path, done: bool, total) -> None:
    out.write_text(
        json.dumps(
            {
                "source": LIST_ENDPOINT,
                "city_id": city["id"],
                "city_name": city["name"],
                "crawled_at": datetime.now().isoformat(timespec="seconds"),
                "city_total_reported": total,
                "complete": done,
                "count": len(rows),
                "hotels": rows,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


async def paginate(
    page, collector, city: dict, out: Path, total, max_pages: int,
    extra_filters: list[dict] | None = None, seen: set[str] | None = None,
) -> set[str]:
    """Phát lại fetchHotelList, tăng dần pageIndex cho tới khi hết.

    `extra_filters`: khi crawl 1 mảnh của bộ chia (build_partition), áp thêm
    filter này vào body. `seen`: tập trip_hotel_id đã thấy — truyền chung
    qua nhiều mảnh để khỏi tính trùng vào % tiến độ. Trả về `seen` đã cập nhật.
    """
    tpl = collector.template
    body = json.loads(tpl["post_data"])
    if extra_filters:
        body["filters"] = _apply_extra(body.get("filters", []), extra_filters)
    paging = body.setdefault("paging", {})
    orig_size = paging.get("pageSize") or 10
    paging["pageSize"] = config.API_PAGE_SIZE
    # Mảnh riêng (có extra_filters) không có trang 1 SSR tương ứng — phải
    # bắt đầu lại từ trang 1, không tiếp nối pageIndex của request mẫu gốc.
    page_index = 0 if extra_filters else int(paging.get("pageIndex") or 1)

    if seen is None:
        seen = {r["trip_hotel_id"] for r in collector.rows}
    leaf_start = len(seen)   # để tính riêng số MỚI của mảnh này, không lẫn seen toàn cục
    empty_streak = 0
    size_fallback_done = False
    label = _fmt_extra(extra_filters)

    print(f"  • {label}: gọi API phân trang (pageSize={config.API_PAGE_SIZE}, "
          f"tối đa {max_pages} trang)…")

    for n in range(1, max_pages + 1):
        page_index += 1
        paging["pageIndex"] = page_index
        # Server dùng danh sách này để khỏi trả lại KS đã hiện. Giữ ~300 id
        # gần nhất: đủ để không lặp, mà body không phình tới vài trăm KB.
        body.setdefault("hotelIdFilter", {})["hotelAldyShown"] = list(seen)[-300:]

        res = await page.evaluate(
            PAGE_FETCH_JS,
            {"url": tpl["url"], "headers": tpl["headers"],
             "body": json.dumps(body, ensure_ascii=False)},
        )

        if res["status"] != 200:
            print(f"    ! Trang {page_index}: HTTP {res['status']}. Dừng lại.")
            (collector.dump_dir / f"fail_page_{page_index}.txt").write_text(
                res["text"][:20000], encoding="utf-8")
            break

        try:
            payload = json.loads(res["text"])
        except Exception:
            print(f"    ! Trang {page_index}: response không phải JSON. Dừng lại.")
            (collector.dump_dir / f"fail_page_{page_index}.txt").write_text(
                res["text"][:20000], encoding="utf-8")
            break

        entries = ((payload.get("data") or {}).get("hotelList")) or []
        new = 0
        for entry in entries:
            row = parse_hotel(entry, city["name"])
            if row and row["trip_hotel_id"] not in seen:
                seen.add(row["trip_hotel_id"])
                collector.rows.append(row)
                new += 1

        # pageSize lớn không được chấp nhận → lùi về giá trị gốc, thử lại.
        # CHỈ xét ở lượt đầu: trang rỗng ở giữa chừng là do hết KS thật,
        # lùi pageSize lúc đó chỉ tốn thêm một lượt gọi vô ích.
        if not entries and n == 1 and not size_fallback_done and config.API_PAGE_SIZE != orig_size:
            size_fallback_done = True
            paging["pageSize"] = orig_size
            page_index -= 1
            print(f"    · pageSize={config.API_PAGE_SIZE} không có kết quả, "
                  f"lùi về {orig_size} và thử lại.")
            continue

        leaf_seen = len(seen) - leaf_start   # số duy nhất mảnh NÀY đã góp, không lẫn mảnh trước
        empty_streak = empty_streak + 1 if new == 0 else 0
        # In dày ở đầu để thấy ngay là chạy được, sau đó thưa lại cho đỡ rối.
        if n <= 5 or n % 10 == 0 or new == 0:
            pct = f" ({leaf_seen * 100 // total}% của mảnh này)" if total else ""
            print(f"    trang {page_index}: +{new} mới → {leaf_seen}/{total}{pct}"
                  f" | tổng gộp toàn thành phố: {len(seen)}")

        if n % config.CHECKPOINT_EVERY == 0:
            _save(city, dedupe(collector.rows), out, False, total)

        if is_last_page(payload):
            print(f"    · Server báo hết trang ở trang {page_index}.")
            break
        if empty_streak >= 3:
            print("    · 3 trang liền không có KS mới — coi như hết.")
            break
        if total and leaf_seen >= total:
            break

        await asyncio.sleep(random.uniform(config.API_MIN_DELAY, config.API_MAX_DELAY))

    return seen


async def crawl_one_city(ctx, city: dict, out: Path, max_pages: int) -> list[dict]:
    page = await ctx.new_page()
    collector = HotelCollector(city["name"])
    page.on("response", lambda r: asyncio.create_task(collector.on_response(r)))

    tomorrow = datetime.now() + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    url = build_list_url(city, tomorrow.strftime("%Y-%m-%d"), day_after.strftime("%Y-%m-%d"))

    print(f"\n=== {city['name']} (cityId={city['id']}) ===")
    await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(4000)

    # --- Trang 1: nằm sẵn trong HTML (SSR), không đi qua API ---
    html = await page.content()
    (config.HTML_DIR / f"list_{city['id']}.html").write_text(html, encoding="utf-8")
    ssr_rows, meta = extract_from_html(html, city_name=city["name"])
    total = meta.get("total")
    if ssr_rows:
        collector.rows.extend(ssr_rows)
        print(f"  • HTML (trang 1): {len(ssr_rows)} khách sạn | cả thành phố: {total}")
    else:
        print(f"  ! Không bóc được dữ liệu từ HTML — xem output/html/list_{city['id']}.html")

    # --- Cuộn vài vòng CHỈ để bắt mẫu request (kèm token) ---
    for _ in range(8):
        if collector.template:
            break
        try:
            await page.evaluate(SCROLL_TO_BOTTOM_JS)
        except Exception:
            break
        await page.wait_for_timeout(config.SCROLL_PAUSE_MS)

    if collector.template:
        tpl = collector.template
        base_filters = json.loads(tpl["post_data"]).get("filters", [])
        baseline = await probe_count(page, tpl, base_filters, []) or total

        if baseline and baseline > config.PARTITION_CAP:
            print(f"  • ~{baseline} KS, vượt ngưỡng chặn mềm {config.PARTITION_CAP} "
                  f"→ dò bộ chia truy vấn…")
            plan = await build_partition(page, tpl, base_filters, [], baseline)
            print(f"    chia thành {len(plan)} mảnh: "
                  + ", ".join(f"{_fmt_extra(e)}~{c}" for e, c in plan))
            seen = {r["trip_hotel_id"] for r in collector.rows}
            for extra, cnt in plan:
                seen = await paginate(page, collector, city, out, cnt, max_pages,
                                       extra_filters=extra, seen=seen)
                _save(city, dedupe(collector.rows), out, False, baseline)
        else:
            await paginate(page, collector, city, out, baseline, max_pages)
    else:
        print("  ⚠ Không bắt được mẫu request fetchHotelList sau 8 vòng cuộn.")
        print(f"    Xem {collector.dump_dir} và ảnh chụp bên dưới rồi gửi lại cho em.")
        await page.screenshot(path=str(config.HTML_DIR / f"debug_{city['id']}_notpl.png"))

    result = dedupe(collector.rows)
    pct = f" ({len(result) * 100 // total}% của {total})" if total else ""
    print(f"  → {len(result)} khách sạn duy nhất{pct}")
    await page.close()
    return result


async def main(args: argparse.Namespace) -> None:
    cities = config.VN_CITIES
    if args.city_id:
        cities = [c for c in cities if c["id"] == args.city_id]
    elif args.city:
        cities = [c for c in cities if args.city.lower() in c["name"].lower()]
    if not cities:
        raise SystemExit("Không khớp thành phố nào trong config.VN_CITIES.")

    max_pages = args.max_pages or config.MAX_API_PAGES

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE_DIR),
            headless=config.HEADLESS,
            locale=config.LOCALE,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, city in enumerate(cities):
            out = config.DATA_DIR / f"api_hotels_{city['id']}_{stamp}.json"
            rows = await crawl_one_city(ctx, city, out, max_pages)
            if rows:
                _save(city, rows, out, True, None)
                print(f"  → lưu {out}")
                print(f"    nạp DB: python src/db/loader.py {out.name}")

            if i < len(cities) - 1:
                await asyncio.sleep(random.uniform(config.MIN_DELAY * 2, config.MAX_DELAY * 2))

        await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", help="lọc theo tên (khớp một phần, không phân biệt hoa thường)")
    ap.add_argument("--city-id", type=int, help="chỉ chạy đúng 1 cityId")
    ap.add_argument("--max-pages", type=int, help="giới hạn số trang API (chạy thử nhanh)")
    args = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args))
