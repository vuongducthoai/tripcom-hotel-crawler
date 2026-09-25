"""Ghép khách sạn v2 với Tripadvisor (API chính thức) → bảng v2.hotel_tripadvisor.

Chỉ lấy 3 thứ: điểm (thang 5), số đánh giá, link trang Tripadvisor.

    set TRIPADVISOR_API_KEY=<key của anh>        (PowerShell: $env:TRIPADVISOR_API_KEY="...")
    python scripts/tripadvisor_match.py --limit 5 --dry-run   # thử 5 hotel, không ghi DB
    python scripts/tripadvisor_match.py --limit 50            # 50 hotel đầu chưa ghép
    python scripts/tripadvisor_match.py                       # tất cả hotel chưa ghép
    python scripts/tripadvisor_match.py --city Copenhagen     # một thành phố
    python scripts/tripadvisor_match.py --refresh             # chỉ cập nhật điểm/số đánh giá
                                                              # cho hotel đã ghép (1 lần gọi/hotel)

Mỗi hotel mới: 1 lần Location Search (tên + tọa độ) + 1 lần Location Details.
Hotel đã ghép / đã xác định không có thì bỏ qua (chạy lại bao nhiêu lần cũng được).

Quy tắc ghép (kiểm tra trước khi lưu):
  matched  : tên giống ≥ 0.80 và cách nhau ≤ 300 m
  review   : có ứng viên nhưng tên giống 0.55–0.80 hoặc cách 300–1500 m → người xem lại
  no_match : không có ứng viên nào đủ gần / đủ giống
Điểm phải trong 0–5, số đánh giá ≥ 0, link phải thuộc tripadvisor.* (DB cũng CHECK lại).

Giới hạn: --max-calls (mặc định 9000/lần chạy, dưới mức 10.000 lần tìm kiếm/ngày
của Tripadvisor). Gặp 429 (hết hạn mức) hoặc 401/403 (sai key) thì dừng ngay.
Kết quả thêm vào output/tripadvisor/<thời điểm>.csv để duyệt các dòng 'review'.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config

API = "https://api.content.tripadvisor.com/api/v1"
MATCH_SIM, MATCH_DIST = 0.80, 300
REVIEW_SIM, REVIEW_DIST = 0.55, 1500
# Chữ chung chung trong tên khách sạn — bỏ đi trước khi so tên
GENERIC = {"hotel", "hotels", "the", "by", "and", "a", "an", "resort", "inn", "suites", "suite",
           "apartments", "apartment", "hostel", "khach", "san", "spa", "collection", "&"}


class StopRun(Exception):
    """Hết hạn mức / sai key — dừng cả lượt chạy."""


# ------------------------------------------------------------------ so khớp
def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("ø", "o").replace("æ", "ae").replace("å", "a").replace("đ", "d")
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in GENERIC]
    return " ".join(words)


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    wa, wb = set(na.split()), set(nb.split())
    overlap = len(wa & wb) / min(len(wa), len(wb))   # "Scandic Norreport" ⊂ "Scandic Norreport Copenhagen"
    return round(max(ratio, overlap * 0.95), 3)


def distance_m(lat1, lng1, lat2, lng2) -> int | None:
    try:
        lat1, lng1, lat2, lng2 = map(float, (lat1, lng1, lat2, lng2))
    except (TypeError, ValueError):
        return None
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))


def classify(similarity: float, distance: int | None) -> str:
    if distance is None:
        return "review" if similarity >= MATCH_SIM else "no_match"
    if similarity >= MATCH_SIM and distance <= MATCH_DIST:
        return "matched"
    if similarity >= REVIEW_SIM and distance <= REVIEW_DIST:
        return "review"
    return "no_match"


def parse_details(data: dict) -> dict:
    """Lấy rating / num_reviews / web_url từ Location Details, kiểm tra khoảng giá trị."""
    rating = review_count = None
    try:
        rating = float(data.get("rating")) if data.get("rating") not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    if rating is not None and not 0 <= rating <= 5:
        rating = None
    try:
        review_count = int(str(data.get("num_reviews") or "").replace(",", "")) \
            if data.get("num_reviews") not in (None, "") else None
    except ValueError:
        review_count = None
    if review_count is not None and review_count < 0:
        review_count = None
    url = data.get("web_url") or None
    if url and not re.match(r"^https://([a-z0-9-]+\.)*tripadvisor\.[a-z.]+/", url):
        url = None
    return {"rating": rating, "review_count": review_count, "tripadvisor_url": url,
            "tripadvisor_name": data.get("name"),
            "latitude": data.get("latitude"), "longitude": data.get("longitude")}


# ------------------------------------------------------------------ API
class Client:
    def __init__(self, key: str, max_calls: int, pause: float, http=None):
        import httpx
        self.key = key
        self.max_calls = max_calls
        self.pause = pause
        self.calls = 0
        self.http = http or httpx.Client(timeout=20, headers={"accept": "application/json"})

    def get(self, path: str, **params) -> dict:
        if self.calls >= self.max_calls:
            raise StopRun(f"đã dùng hết --max-calls={self.max_calls} cho lượt này")
        self.calls += 1
        response = self.http.get(f"{API}{path}", params={"key": self.key, "language": "en", **params})
        if response.status_code == 429:
            raise StopRun("Tripadvisor báo 429 — hết hạn mức ngày/tháng, chạy lại sau")
        if response.status_code in (401, 403):
            raise StopRun(f"Tripadvisor báo {response.status_code} — kiểm tra API key / domain được phép")
        response.raise_for_status()
        time.sleep(self.pause)
        return response.json()

    def search(self, name: str, lat, lng) -> list[dict]:
        params = {"searchQuery": name, "category": "hotels"}
        if lat is not None and lng is not None:
            params["latLong"] = f"{lat},{lng}"
        return (self.get("/location/search", **params).get("data")) or []

    def details(self, location_id) -> dict:
        return self.get(f"/location/{location_id}/details", currency="USD")


def match_hotel(client: Client, hotel: dict) -> dict:
    """Tìm + lấy chi tiết cho một hotel; trả về dòng để ghi bảng hotel_tripadvisor."""
    row = {"hotel_id": hotel["hotel_id"], "trip_hotel_id": hotel["trip_hotel_id"],
           "tripadvisor_location_id": None, "match_status": "no_match", "name_similarity": None,
           "distance_m": None, "tripadvisor_name": None, "rating": None, "review_count": None,
           "tripadvisor_url": None, "last_error": None}
    candidates = client.search(hotel["name"], hotel["latitude"], hotel["longitude"])
    if not candidates:
        return row
    # Ứng viên tên giống nhất (API đã ưu tiên gần tọa độ)
    best = max(candidates[:10], key=lambda c: name_similarity(hotel["name"], c.get("name", "")))
    similarity = name_similarity(hotel["name"], best.get("name", ""))
    if similarity < REVIEW_SIM:
        row.update(name_similarity=similarity, tripadvisor_name=best.get("name"))
        return row
    details = parse_details(client.details(best["location_id"]))
    distance = distance_m(hotel["latitude"], hotel["longitude"], details["latitude"], details["longitude"])
    status = classify(similarity, distance)
    row.update(tripadvisor_location_id=int(best["location_id"]), match_status=status,
               name_similarity=similarity, distance_m=distance,
               tripadvisor_name=details["tripadvisor_name"] or best.get("name"))
    if status != "no_match":
        row.update(rating=details["rating"], review_count=details["review_count"],
                   tripadvisor_url=details["tripadvisor_url"])
    else:
        row["tripadvisor_location_id"] = None
    return row


# ------------------------------------------------------------------ DB
def pending_hotels(cur, city: str | None, refresh: bool, limit: int | None) -> list[dict]:
    where = ["h.latitude IS NOT NULL" if not refresh else "TRUE"]
    params: list = []
    if refresh:
        where.append("ta.match_status IN ('matched', 'review')")
    else:
        where.append("(ta.hotel_id IS NULL OR ta.match_status = 'error')")
    if city:
        where.append("EXISTS (SELECT 1 FROM v2.city_i18n ci WHERE ci.city_id = h.city_id AND ci.name ILIKE %s)")
        params.append(f"%{city}%")
    sql = f"""
        SELECT h.id, h.trip_hotel_id, h.latitude, h.longitude,
               COALESCE(en.name, vi.name) AS name, ta.tripadvisor_location_id
        FROM v2.hotels h
        LEFT JOIN v2.hotel_i18n en ON en.hotel_id = h.id AND en.locale = 'en'
        LEFT JOIN v2.hotel_i18n vi ON vi.hotel_id = h.id AND vi.locale = 'vi'
        LEFT JOIN v2.hotel_tripadvisor ta ON ta.hotel_id = h.id
        WHERE {' AND '.join(where)} AND COALESCE(en.name, vi.name) IS NOT NULL
        ORDER BY h.id""" + (f" LIMIT {int(limit)}" if limit else "")
    cur.execute(sql, params)
    return [{"hotel_id": r[0], "trip_hotel_id": r[1], "latitude": r[2], "longitude": r[3],
             "name": r[4], "tripadvisor_location_id": r[5]} for r in cur.fetchall()]


UPSERT = """
INSERT INTO v2.hotel_tripadvisor (hotel_id, trip_hotel_id, tripadvisor_location_id, match_status,
    name_similarity, distance_m, tripadvisor_name, rating, review_count, tripadvisor_url,
    searched_at, refreshed_at, last_error)
VALUES (%(hotel_id)s, %(trip_hotel_id)s, %(tripadvisor_location_id)s, %(match_status)s,
    %(name_similarity)s, %(distance_m)s, %(tripadvisor_name)s, %(rating)s, %(review_count)s,
    %(tripadvisor_url)s, now(), CASE WHEN %(rating)s IS NOT NULL OR %(review_count)s IS NOT NULL
    THEN now() END, %(last_error)s)
ON CONFLICT (hotel_id) DO UPDATE SET
    tripadvisor_location_id = EXCLUDED.tripadvisor_location_id, match_status = EXCLUDED.match_status,
    name_similarity = EXCLUDED.name_similarity, distance_m = EXCLUDED.distance_m,
    tripadvisor_name = EXCLUDED.tripadvisor_name, rating = EXCLUDED.rating,
    review_count = EXCLUDED.review_count, tripadvisor_url = EXCLUDED.tripadvisor_url,
    searched_at = EXCLUDED.searched_at, refreshed_at = EXCLUDED.refreshed_at,
    last_error = EXCLUDED.last_error"""

REFRESH = """
UPDATE v2.hotel_tripadvisor SET rating = %(rating)s, review_count = %(review_count)s,
    tripadvisor_url = COALESCE(%(tripadvisor_url)s, tripadvisor_url), refreshed_at = now(), last_error = NULL
WHERE hotel_id = %(hotel_id)s"""


# ------------------------------------------------------------------ main
def main(args) -> int:
    key = os.getenv("TRIPADVISOR_API_KEY")
    if not key:
        print("Thiếu API key: đặt biến môi trường TRIPADVISOR_API_KEY (PowerShell: $env:TRIPADVISOR_API_KEY=\"...\")")
        return 1
    import psycopg2
    conn = psycopg2.connect(config.dsn())
    with conn.cursor() as cur:
        hotels = pending_hotels(cur, args.city, args.refresh, args.limit)
    mode = "cập nhật điểm" if args.refresh else "ghép mới"
    per_hotel = 1 if args.refresh else 2
    print(f"{len(hotels)} hotel cần {mode} · tối đa ~{len(hotels) * per_hotel} lần gọi API "
          f"(giới hạn lượt này {args.max_calls})" + (" · DRY RUN, không ghi DB" if args.dry_run else ""))
    client = Client(key, args.max_calls, args.pause)
    out_dir = config.OUTPUT_DIR / "tripadvisor"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / f"{datetime.now():%Y%m%d_%H%M%S}{'_refresh' if args.refresh else ''}.csv"
    counts: dict[str, int] = {}
    fields = ["trip_hotel_id", "name", "match_status", "tripadvisor_location_id", "tripadvisor_name",
              "name_similarity", "distance_m", "rating", "review_count", "tripadvisor_url", "last_error"]
    with report.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        try:
            for index, hotel in enumerate(hotels, 1):
                try:
                    if args.refresh:
                        row = {"hotel_id": hotel["hotel_id"], "trip_hotel_id": hotel["trip_hotel_id"],
                               "match_status": "refreshed",
                               **parse_details(client.details(hotel["tripadvisor_location_id"]))}
                    else:
                        row = match_hotel(client, hotel)
                except StopRun:
                    raise
                except Exception as exc:  # lỗi mạng / API lẻ → ghi 'error', chạy tiếp
                    row = {"hotel_id": hotel["hotel_id"], "trip_hotel_id": hotel["trip_hotel_id"],
                           "tripadvisor_location_id": None, "match_status": "error", "name_similarity": None,
                           "distance_m": None, "tripadvisor_name": None, "rating": None,
                           "review_count": None, "tripadvisor_url": None,
                           "last_error": f"{type(exc).__name__}: {exc}"[:300]}
                counts[row["match_status"]] = counts.get(row["match_status"], 0) + 1
                writer.writerow({**row, "name": hotel["name"]})
                if not args.dry_run and not (args.refresh and row["match_status"] == "error"):
                    with conn.cursor() as cur:
                        cur.execute(REFRESH if args.refresh else UPSERT, row)
                    conn.commit()
                if index % 25 == 0:
                    print(f"  {index}/{len(hotels)} · {counts} · {client.calls} lần gọi", flush=True)
        except StopRun as why:
            print(f"\nDỪNG: {why}")
    conn.close()
    print(f"\nKết quả: {counts} · {client.calls} lần gọi API")
    print(f"File để duyệt (lọc cột match_status = review): {report}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--city", help="lọc theo tên thành phố trong v2, vd Copenhagen")
    ap.add_argument("--limit", type=int, help="chỉ N hotel đầu")
    ap.add_argument("--refresh", action="store_true", help="chỉ cập nhật điểm/số đánh giá cho hotel đã ghép")
    ap.add_argument("--max-calls", type=int, default=9000, help="số lần gọi API tối đa mỗi lượt (mặc định 9000)")
    ap.add_argument("--pause", type=float, default=0.2, help="nghỉ giữa các lần gọi, giây")
    ap.add_argument("--dry-run", action="store_true", help="gọi API, xuất CSV, KHÔNG ghi DB")
    sys.exit(main(ap.parse_args()))
