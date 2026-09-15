"""Trích dữ liệu khách sạn từ response JSON thật của Trip.com (fetchDynamicRefreshList).

Đây là dữ liệu giàu hơn hẳn parse CSS: có sao, điểm review chi tiết, toạ độ,
giá đã tính thuế — lấy thẳng từ nguồn, không phải đoán qua class CSS.
"""
from __future__ import annotations

import json
import re


def _num(raw: str | None) -> float | None:
    """'9,2' hoặc '584 đánh giá' → số. None nếu không parse được."""
    if not raw:
        return None
    m = re.search(r"[\d]+(?:[.,]\d+)?", str(raw))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def parse_hotel(entry: dict, city_name: str | None = None) -> dict | None:
    """1 phần tử trong data.hotelList → dict khớp cột bảng `hotels`."""
    info = entry.get("hotelInfo") or {}
    summary = info.get("summary") or {}
    hotel_id = summary.get("hotelId")
    if not hotel_id:
        return None

    name_info = info.get("nameInfo") or {}
    star = (info.get("hotelStar") or {}).get("star")
    comment = info.get("commentInfo") or {}
    position = info.get("positionInfo") or {}

    lat = lon = None
    for coord in position.get("mapCoordinate") or []:
        if coord.get("coordinateType") == 1:  # WGS84 gốc
            lat, lon = coord.get("latitude"), coord.get("longitude")
            break

    images = [
        img.get("url")
        for img in (info.get("hotelImages") or {}).get("multiImgs") or []
        if img.get("url")
    ]

    price = currency = None
    rooms = entry.get("roomInfo") or []
    if rooms:
        price_info = (rooms[0].get("priceInfo") or {})
        price = price_info.get("price")
        currency = price_info.get("currency")

    address = position.get("address") or position.get("positionDesc")

    return {
        "trip_hotel_id": str(hotel_id),
        "name": name_info.get("name"),
        "name_en": name_info.get("enName"),
        "url": f"https://vn.trip.com/hotels/detail/?hotelId={hotel_id}",
        "address": address,
        "latitude": float(lat) if lat else None,
        "longitude": float(lon) if lon else None,
        "star_rating": float(star) if star else None,
        "score": comment.get("commentScore"),          # loader tự parse dấu phẩy
        "review_score": _num(comment.get("commentScore")),
        "review_count": int(_num(comment.get("commenterNumber")) or 0) or None,
        "price_value": float(price) if price is not None else None,
        "currency": currency or "VND",
        "city_name": city_name or position.get("cityName"),
        "images": images[:5],
    }


def parse_response(payload: dict, city_name: str | None = None) -> list[dict]:
    """Toàn bộ response JSON → list các dict khách sạn (bỏ qua entry lỗi)."""
    hotel_list = ((payload.get("data") or {}).get("hotelList")) or []
    out = []
    for entry in hotel_list:
        row = parse_hotel(entry, city_name)
        if row:
            out.append(row)
    return out


def is_last_page(payload: dict) -> bool:
    addi = ((payload.get("data") or {}).get("hotelListAddtionInfo")) or {}
    return bool(addi.get("isLastPage"))


def total_count(payload: dict) -> int | None:
    addi = ((payload.get("data") or {}).get("hotelListAddtionInfo")) or {}
    return addi.get("hotelTotalCount")


# ------------------------------------------------------------------ SSR HTML
# Trang 1 KHÔNG đến từ API — Trip.com nhúng sẵn vào HTML qua Next.js
# (self.__next_f.push). Phải bóc từ đó, nếu không sẽ tưởng là crawl rỗng.

_NEXT_F = re.compile(r'self\.__next_f\.push\(\[1\s*,\s*("(?:[^"\\]|\\.)*")')


def _next_f_text(html: str) -> str:
    """Ghép lại toàn bộ flight data của Next.js, đã giải mã escape."""
    parts = []
    for m in _NEXT_F.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))
        except Exception:
            continue
    return "".join(parts)


def _balanced_object(s: str, start: int) -> str | None:
    """Cắt đúng một object JSON cân ngoặc từ vị trí '{' đầu tiên."""
    depth = 0
    in_str = esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return None


def extract_from_html(html: str, city_name: str | None = None) -> tuple[list[dict], dict]:
    """HTML trang danh sách → (danh sách khách sạn, meta).

    meta: {'total': tổng KS của thành phố, 'is_last_page': bool}
    """
    text = _next_f_text(html)
    i = text.find('"initListData"')
    if i < 0:
        return [], {}
    j = text.find("{", i)
    if j < 0:
        return [], {}
    blob = _balanced_object(text, j)
    if not blob:
        return [], {}
    try:
        data = json.loads(blob)
    except Exception:
        return [], {}

    rows = parse_response({"data": data}, city_name=city_name)
    addi = data.get("hotelListAddtionInfo") or {}
    return rows, {
        "total": addi.get("hotelTotalCount"),
        "is_last_page": bool(addi.get("isLastPage")),
    }


# --------------------------------------------------------- dò nguồn chưa biết
def find_hotel_lists(obj, path: str = "") -> list[tuple[str, list]]:
    """Quét sâu một JSON bất kỳ, tìm mảng chứa khách sạn.

    Dùng để phát hiện endpoint phân trang mà mình chưa biết tên.
    """
    found: list[tuple[str, list]] = []

    def looks_like_hotel(item) -> bool:
        if not isinstance(item, dict):
            return False
        if "hotelInfo" in item:
            return True
        if "hotelId" in item:          # dạng phẳng, không lồng trong summary
            return True
        summary = item.get("summary") or item.get("hotelBaseInfo")
        return isinstance(summary, dict) and "hotelId" in summary

    def walk(o, p: str, depth: int) -> None:
        if depth > 8:
            return
        if isinstance(o, list):
            if o and looks_like_hotel(o[0]):
                found.append((p, o))
                return
            for k, v in enumerate(o[:5]):
                walk(v, f"{p}[{k}]", depth + 1)
        elif isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{p}.{k}" if p else k, depth + 1)

    walk(obj, path, 0)
    return found


def dedupe(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for r in rows:
        key = r["trip_hotel_id"]
        if key not in best or len(r.get("images") or []) > len(best[key].get("images") or []):
            best[key] = r
    return list(best.values())
