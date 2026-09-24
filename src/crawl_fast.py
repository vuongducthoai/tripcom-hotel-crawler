"""Crawler tĩnh: tải trang bằng HTTP thuần, không mở Chromium.

Vì sao có file này: crawl_detail.py mở Chromium thật nên mỗi worker ăn ~1GB
RAM; chạy 3 worker là máy đuối, Chromium tự đóng giữa chừng và cả lô hỏng.
Trang chi tiết Trip.com là Next.js render sẵn ở server, nên phần lớn dữ liệu
(sao, toạ độ, mô tả, tiện ích, chính sách, số phòng) đã nằm trong HTML dưới
dạng flight data `self.__next_f.push(...)` — tải HTML rồi bóc ra là đủ, không
cần trình duyệt.

HTML SSR có thể chứa cấu trúc loại phòng (`physicRoomMap`,
`roomPopInfo`) nhưng không đảm bảo có giá/gói bán. Mặc định crawler chỉ
lấy SSR để không đụng endpoint phòng nhạy cảm. Muốn enrichment giá,
gói phòng và nearby bằng mẫu XHR thì thêm `--enrich-apis`.

Dùng lại phiên đăng nhập sẵn có (cookie xuất từ browser_profile). Không giả
mạo vân tay TLS, không xoay proxy: gọi chậm, một IP, gặp chặn thì dừng.

    python src\\crawl_fast.py --export-cookies              # làm 1 lần
    python src\\crawl_fast.py --hotel-id 744865             # thử 1 khách sạn
    python src\\crawl_fast.py --ids-file output\\ids.txt --delay 2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config                                   # noqa: E402
import raw_store                                # noqa: E402
import fast_api                                # noqa: E402
from block_detect import blocked_reason as api_blocked    # noqa: E402
from api_extract import _next_f_text            # noqa: E402
from detail_extract import extract_detail       # noqa: E402

JSON_DECODER = json.JSONDecoder()
DETAIL_BLOCK_URL = "embedded:hotel-detail-response"
MARKET_HOST = {"vi-VN": "vn.trip.com", "en-US": "www.trip.com"}

# Nhận diện bị chặn — giống hệt crawl_detail.py để hai crawler hành xử như nhau
BLOCK_MARKERS = ("htlSpiderActionErrorCode", "Antibot", "/account/signin",
                 "c-slide-captcha", "challenge_page", "Antibot-Gray-ip")

JSON_LD = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)
META_DESC = re.compile(
    r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])'
    r'[^>]+content=["\'](.*?)["\']', re.S | re.I)
SCRIPT = re.compile(r'<script\b[^>]*>(.*?)</script>', re.S | re.I)
NEXT_PUSH = re.compile(r'(?:self|window)\.__next_f\.push\(\s*')


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
        start += 1           # raw_decode không tự bỏ khoảng trắng
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
    """hotelFacilityPopV2 trong SSR → khối 'embedded:hotel-facilities'.

    Bản Chromium lấy tiện nghi bằng cách mở popup rồi đọc DOM. Cùng dữ liệu đó
    đã nằm sẵn trong flight data, chỉ khác cách xếp: `hotelFacility` là danh
    sách nhóm (Internet, Bãi đỗ xe…), mỗi nhóm có `categoryList[].list[]`;
    `hotelNormalFacilityList` là các mục lẻ không thuộc nhóm nào.
    """
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
        packets.append({"url": DETAIL_BLOCK_URL, "method": "EMBEDDED",
                        "status": 200, "response": block})
        tien_nghi = facilities_from_detail(block)
        if tien_nghi:
            packets.append({"url": "embedded:hotel-facilities", "method": "EMBEDDED",
                            "status": 200, "response": tien_nghi})
    for value in json_ld_blocks(html):
        packets.append({"url": "embedded:json-ld", "method": "EMBEDDED",
                        "status": 200, "response": value})
    desc = meta_description(html)
    if desc:
        packets.append({"url": "embedded:page-meta", "method": "EMBEDDED",
                        "status": 200, "response": {"description": desc}})
    seen_rooms: set[str] = set()
    for chunk in next_f_chunks(html):
        for payload in room_payloads(chunk):
            signature = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
            if signature in seen_rooms:
                continue
            seen_rooms.add(signature)
            packets.append({"url": "embedded:hotel-rooms", "method": "EMBEDDED",
                            "status": 200, "response": payload})
    return packets


# ------------------------------------------------------------------- cookie
def cookie_file(locale: str, currency: str) -> Path:
    return config.OUTPUT_DIR / f"cookies_{locale}_{currency.upper()}.json"


async def export_cookies(locale: str, currency: str) -> Path:
    """Mở browser_profile đúng một lần để lấy cookie, rồi đóng."""
    from playwright.async_api import async_playwright

    target = cookie_file(locale, currency)
    async with async_playwright() as pw:
        launch_options = {
            "headless": True,
        }
        proxy = config.browser_proxy()
        if proxy:
            launch_options["proxy"] = proxy
        context = await pw.chromium.launch_persistent_context(
            str(config.profile_dir(locale, currency)), **launch_options)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://{MARKET_HOST.get(locale, 'www.trip.com')}/",
                            wait_until="domcontentloaded", timeout=45000)
            cookies = await context.cookies()
        finally:
            await context.close()
    target.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    return target


def load_cookies(locale: str, currency: str) -> dict:
    path = cookie_file(locale, currency)
    if not path.exists():
        raise SystemExit(
            f"Chưa có cookie: {path}\n→ chạy: python src/crawl_fast.py --export-cookies")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in data if c.get("name")}


# ------------------------------------------------------------------- tải trang
def detail_url(hotel_id: str, locale: str, currency: str,
               checkin: str, checkout: str) -> str:
    host = MARKET_HOST.get(locale, "www.trip.com")
    return (f"https://{host}/hotels/detail/?hotelId={hotel_id}"
            f"&checkIn={checkin}&checkOut={checkout}"
            f"&adult=2&children=0&crn=1&curr={currency.upper()}&locale={locale}")


def headers_for(locale: str) -> dict:
    return {
        "User-Agent": config.USER_AGENT if hasattr(config, "USER_AGENT") else
                      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8" if locale.startswith("vi")
                           else "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


def fetch_one(client, hotel_id: str, locale: str, currency: str,
              checkin: str, checkout: str, save_html: bool = False,
              mau: dict | None = None,
              visitor_id: str | None = None) -> tuple[dict, str | None]:
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

    # Gọi lại các API mà trang tự gọi bằng JavaScript (phòng, giá, địa điểm,
    # album ảnh) — dùng mẫu request bắt được từ lần cào bằng Chromium.
    if packets and mau:
        detail = next((p["response"] for p in packets
                       if p["url"] == DETAIL_BLOCK_URL), None)
        ctx = fast_api.context_from_detail(detail)
        for ten_api in sorted(mau.get("apis") or {}):
            try:
                api_url, api_headers, body = fast_api.payload_for(
                    mau, ten_api, hotel_id, checkin, checkout, ctx,
                    visitor_id=visitor_id)
                r = client.post(api_url, json=body,
                                headers={**headers_for(locale), **api_headers})
                value = r.json()
            except Exception as exc:
                print(f"       API {ten_api} lỗi: {type(exc).__name__}: {exc}")
                continue
            vi_sao = api_blocked(value)
            if vi_sao:
                # API trả 200 nhưng nội dung là thông báo chặn (có khi giấu
                # trong mảng byte XOR 0x0A). Dừng chứ không đổi IP thử lại.
                reason = f"API {ten_api} bị chặn: {vi_sao}"
                packets = []
                break
            packets.append({"url": api_url.split("?", 1)[0], "method": "POST",
                            "status": r.status_code, "response": value})

    normalized = extract_detail(packets, hotel_id, url, currency, locale)
    normalized["success"] = bool(packets) and reason is None
    normalized["check_in"], normalized["check_out"] = checkin, checkout
    normalized["crawled_at"] = datetime.now().isoformat(timespec="seconds")
    normalized["locale"], normalized["currency"] = locale, currency
    normalized["data_mode"] = "api-enriched" if mau else "ssr-static"
    normalized["api_enriched"] = bool(mau)
    normalized["rooms_live"] = any(
        "getHotelRoomListOversea" in str(packet.get("url") or "")
        for packet in packets)
    if reason:
        normalized["error"] = f"Trip.com chặn: {reason}"
    elif not packets:
        normalized["error"] = "HTML không có flight data hotelDetailResponse"

    return {"target": {"hotel_id": hotel_id, "url": url},
            "url": url, "normalized": normalized, "responses": packets}, reason


def save(dump: dict, hotel_id: str, locale: str, currency: str,
         into_raw: bool = False) -> tuple[Path, bool]:
    """Ghi raw. Trả về (đường dẫn, có bỏ qua vì raw cũ đầy đủ hơn không).

    Mặc định ghi vào thư mục RIÊNG `details/raw_fast/`: dữ liệu tĩnh không có
    phòng/giá/địa điểm gần đây, đè lên raw của bản Chromium là mất dữ liệu.
    Chỉ khi `--into-raw` mới ghi vào thư mục raw chính, và kể cả khi đó cũng
    không đè lên một raw cũ đã thành công và giàu hơn.
    """
    root = "raw" if into_raw else "raw_fast"
    folder = config.OUTPUT_DIR / "details" / root / locale / currency.upper()
    folder.mkdir(parents=True, exist_ok=True)
    ok = (dump.get("normalized") or {}).get("success")
    target = folder / f"{hotel_id}.json"

    if ok and into_raw:
        old_quality = _raw_quality(target)
        new_quality = _dump_quality(dump)
        if old_quality > new_quality:
            return target, True          # raw cũ nhiều dữ liệu hơn → giữ nguyên

    name = (f"{hotel_id}.json" if ok else
            f"{hotel_id}.failed.{datetime.now():%Y%m%d_%H%M%S}.json")
    return raw_store.write(folder / name, dump), False


def _dump_quality(dump: dict) -> tuple[int, int, int, int]:
    """Xếp raw theo: thành công, có API phòng, có offer, số packet."""
    normalized = dump.get("normalized") or {}
    responses = dump.get("responses") or []
    if not normalized.get("success") or any(
            api_blocked(packet.get("response")) for packet in responses):
        return (0, 0, 0, 0)
    room_packets = [packet for packet in responses
                    if "getHotelRoomListOversea" in str(packet.get("url") or "")]
    has_offers = False
    for packet in room_packets:
        response = packet.get("response") or {}
        data = response.get("data") or {} if isinstance(response, dict) else {}
        if data.get("saleRoomMap"):
            has_offers = True
            break
    return (1, int(bool(room_packets)), int(has_offers), len(responses))


def _raw_quality(path: Path) -> tuple[int, int, int, int]:
    """Chất lượng raw đang có; raw API không bị SSR tĩnh ghi đè."""
    try:
        return _dump_quality(raw_store.read(path))
    except Exception:
        return (0, 0, 0, 0)


# ---------------------------------------------------------------------- main
def read_ids(args) -> list[str]:
    if args.hotel_id:
        return [str(args.hotel_id)]
    if args.ids_file:
        text = Path(args.ids_file).read_text(encoding="utf-8")
        return [x for x in re.split(r"\s+", text.strip()) if x]
    if args.file:
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        return [str(h["trip_hotel_id"]) for h in payload.get("hotels") or []]
    raise SystemExit("Cần --hotel-id, --ids-file hoặc --file")


def main(args) -> int:
    import httpx

    if args.export_cookies:
        path = asyncio.run(export_cookies(args.locale, args.currency))
        print(f"Đã lưu cookie → {path}")
        return 0

    if args.build_templates:
        folder = config.OUTPUT_DIR / "details" / "raw" / args.locale / args.currency.upper()
        path, apis = fast_api.build_templates(
            folder / f"{args.build_templates}.json", args.locale, args.currency)
        print(f"Đã dựng mẫu {len(apis)} API → {path}")
        for a in apis:
            print("   -", a)
        return 0

    mau = fast_api.load_templates(args.locale, args.currency) if args.enrich_apis else None
    if args.enrich_apis and mau:
        print(f"Dùng mẫu API của khách sạn {mau['hotel_id']} "
              f"({len(mau.get('apis') or {})} API)")
    elif args.enrich_apis:
        raise SystemExit("Chưa có mẫu API. Tạo bằng --build-templates <HOTEL_ID>.")
    else:
        print("Chế độ SSR tĩnh: không gọi API phòng/giá; "
              "dùng --enrich-apis cho lượt enrichment riêng.")

    ids = read_ids(args)
    if args.limit:
        ids = ids[:args.limit]
    checkin = args.checkin or default_stay()[0]
    checkout = args.checkout or default_stay()[1]

    print(f"Crawl tĩnh: {len(ids)} khách sạn | {args.locale}/{args.currency.upper()} "
          f"| {checkin} → {checkout} | {args.concurrency} luồng, "
          f"nghỉ {args.delay}–{args.delay + args.jitter:.1f}s mỗi lượt")

    cookies = load_cookies(args.locale, args.currency)
    dung = threading.Event()          # một luồng gặp chặn → cả đợt dừng
    khoa = threading.Lock()           # in log không bị chen dòng
    dem = {"xong": 0, "hong": 0, "bo_qua": 0}
    ly_do_chan: list[str] = []

    def lam_mot(index: int, hotel_id: str, client) -> None:
        if dung.is_set():
            return
        # Giãn nhịp NGAY TRƯỚC mỗi lượt, kể cả khi chạy nhiều luồng: mỗi luồng
        # tự nghỉ nên tổng nhịp gửi vẫn tỉ lệ với số luồng, không dồn cục.
        time.sleep(random.uniform(args.delay, args.delay + args.jitter))
        if dung.is_set():
            return
        try:
            dump, reason = fetch_one(client, hotel_id, args.locale, args.currency,
                                     checkin, checkout, save_html=args.save_html, mau=mau,
                                     visitor_id=cookies.get("UBT_VID"))
        except Exception as exc:
            with khoa:
                dem["hong"] += 1
                print(f"  [{index}/{len(ids)}] {hotel_id} LỖI MẠNG | {exc}")
            return

        path, bo_qua = save(dump, hotel_id, args.locale, args.currency,
                            into_raw=args.into_raw)
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
                print(f"  [{index}/{len(ids)}] {hotel_id} OK | ảnh={len(n.get('images') or [])}, "
                      f"tiện ích={len(n.get('amenities') or [])}, "
                      f"phòng={len(n.get('rooms') or [])} → {path.name}")
            else:
                dem["hong"] += 1
                print(f"  [{index}/{len(ids)}] {hotel_id} THIẾU | {n.get('error')}")

    client_options = {
        "cookies": cookies,
        "timeout": args.timeout,
        "http2": True,
    }
    proxy_url = config.httpx_proxy_url()
    if proxy_url:
        client_options["proxy"] = proxy_url
        print("Proxy: đang bật cho toàn bộ request HTTP")

    with httpx.Client(**client_options) as client:
        if args.concurrency <= 1:
            for index, hotel_id in enumerate(ids, 1):
                lam_mot(index, hotel_id, client)
                if dung.is_set():
                    break
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                viec = [pool.submit(lam_mot, i, h, client)
                        for i, h in enumerate(ids, 1)]
                for v in viec:
                    v.result()

    done, failed = dem["xong"], dem["hong"]
    if ly_do_chan:
        print("\n  Dừng lại. Trip.com đang chặn — nghỉ vài tiếng rồi chạy lại đúng lệnh này.")
        print(f"  Đã xong trước khi dừng: {done} khách sạn.")
        return 2

    print(f"\nXong: {done} thành công, {failed} hỏng.")
    return 0 if done else 1


def default_stay() -> tuple[str, str]:
    """Thứ Hai của tuần sau nữa, ở 1 đêm — giống hệt crawl_detail.default_stay.

    Tự tính chứ không import từ crawl_detail để khỏi kéo theo Playwright.
    """
    from datetime import date, timedelta
    hom_nay = date.today()
    thu_hai = hom_nay + timedelta(days=(7 - hom_nay.weekday()) % 7 or 7) + timedelta(days=7)
    return thu_hai.isoformat(), (thu_hai + timedelta(days=1)).isoformat()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Crawl trang chi tiết bằng HTTP, không mở trình duyệt")
    ap.add_argument("--hotel-id")
    ap.add_argument("--ids-file")
    ap.add_argument("--file", help="file danh sách của crawl_api.py")
    ap.add_argument("--locale", default="vi-VN")
    ap.add_argument("--currency", default="VND")
    ap.add_argument("--checkin")
    ap.add_argument("--checkout")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=2.0, help="giây nghỉ giữa 2 khách sạn")
    ap.add_argument("--jitter", type=float, default=1.5, help="ngẫu nhiên thêm 0..N giây")
    ap.add_argument("--concurrency", type=int, default=1, choices=(1, 2, 3, 4),
                    help="số luồng chạy song song trên IP trực tiếp (mặc định 1)")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--build-templates", metavar="HOTEL_ID",
                    help="dựng mẫu API từ raw của khách sạn này (raw phải do "
                         "crawl_detail.py cào, có kèm request)")
    ap.add_argument("--enrich-apis", action="store_true",
                    help="gọi lại API trong template để lấy giá/offers/nearby; "
                         "mặc định chỉ cào SSR tĩnh")
    ap.add_argument("--save-html", action="store_true",
                    help="lưu HTML thô vào output/html để soi cấu hình API trong SSR")
    ap.add_argument("--into-raw", action="store_true",
                    help="ghi thẳng vào output/details/raw (mặc định: raw_fast riêng). "
                         "Vẫn không đè lên raw cũ có nhiều dữ liệu hơn")
    ap.add_argument("--export-cookies", action="store_true",
                    help="mở browser_profile 1 lần để lấy cookie phiên đăng nhập")
    raise SystemExit(main(ap.parse_args()))
