"""Chuẩn hoá dữ liệu trang chi tiết Trip.com từ các response JSON.

Trip.com thường đổi tên endpoint nhưng cấu trúc field thay đổi chậm hơn. Bộ
extractor này dựa trên tên/path field, đồng thời giữ raw response riêng để có
thể điều chỉnh parser mà không cần crawl lại.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Any, Iterator

IMAGE_RE = re.compile(r"^https?://[^\s]+(?:\.(?:jpe?g|png|webp|avif)(?:\?|$)|tripcdn)", re.I)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
IMAGE_VARIANT_RE = re.compile(r"_(?:R|Z)_\d+_\d+[^.]*?(?=\.(?:jpe?g|png|webp|avif)$)", re.I)
PARSER_VERSION = 2


def _walk(value: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")


def _first(obj: dict, *keys: str) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    match = NUMBER_RE.search(str(value or ""))
    return float(match.group().replace(",", ".")) if match else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _image_url(obj: dict) -> str | None:
    value = _first(obj, "url", "imageUrl", "imageURL", "imgUrl", "picUrl", "largeUrl")
    if not value:
        return None
    url = str(value)
    if url.startswith("//"):
        url = "https:" + url
    return url if IMAGE_RE.search(url) else None


def _image_key(url: str) -> str:
    """Collapse TripCDN size/watermark variants of the same physical image."""
    clean = urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl()
    return IMAGE_VARIANT_RE.sub("", clean).casefold()


def _room_id(obj: dict) -> str | None:
    value = _first(obj, "roomId", "roomID", "roomTypeId", "roomTypeID", "physicalRoomId")
    return str(value) if value is not None else None


def _stable_room_id(name: str, bed: str | None) -> str:
    raw = f"{name}|{bed or ''}".encode("utf-8")
    return "derived:" + hashlib.sha1(raw).hexdigest()[:20]


def _parse_area(value: Any) -> float | None:
    """'22 ㎡' → 22.0"""
    return _number(value)


def _rooms_from_maps(data: dict) -> dict[str, dict]:
    """Trích room từ payload dạng getHotelRoomList*Oversea.

    Endpoint này KHÔNG trả 1 mảng phòng phẳng — nó tách làm 2 map không
    chung field nào để heuristic dò theo tên field (dưới) tự khớp được:
      - physicRoomMap[id] = tên phòng thật, giường, diện tích (không có giá)
      - saleRoomMap[key]  = giá + gói đặt phòng, trỏ ngược về physicRoomMap
        qua field "physicalRoomId" (KHÔNG có tên phòng — "name" rỗng)
    Phải join tay 2 map theo physicalRoomId, không đi qua _walk() được.
    """
    physic = data.get("physicRoomMap")
    if not isinstance(physic, dict):
        return {}
    sale = data.get("saleRoomMap") if isinstance(data.get("saleRoomMap"), dict) else {}

    best: dict[str, dict] = {}  # physicalRoomId (str) → giá rẻ nhất tìm được
    for entry in sale.values():
        if not isinstance(entry, dict):
            continue
        pid = entry.get("physicalRoomId")
        if pid is None:
            continue
        pid = str(pid)
        price_info = entry.get("priceInfo") or {}
        price = _number(price_info.get("price"))
        if price is None:
            continue
        guests = entry.get("guestCountInfo") or {}
        occupancy = _integer(guests.get("guestCount"))
        total_price_info = entry.get("totalPriceInfo") or {}
        pay_tax = total_price_info.get("payTax") or {}
        tax_amount = _number(pay_tax.get("price"))
        explanation = str(price_info.get("priceExplanation") or "").casefold()
        tax_included = None
        if tax_amount is not None:
            tax_included = tax_amount <= 0
        elif "bao gồm thuế" in explanation or "tax included" in explanation:
            tax_included = True

        current = best.get(pid)
        if current is None or price < current["price"]:
            best[pid] = {
                "price": price,
                "currency": price_info.get("currency") or "VND",
                "max_occupancy": occupancy,
                "tax_included": tax_included,
                "sale_room_id": str(entry.get("id")) if entry.get("id") is not None else None,
            }

    rooms: dict[str, dict] = {}
    for rid, room in physic.items():
        if not isinstance(room, dict):
            continue
        name = room.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        bed = (room.get("bedInfo") or {}).get("title")
        area = _parse_area((room.get("areaInfo") or {}).get("title"))
        price_entry = best.get(str(rid)) or {}
        rooms[str(rid)] = {
            "trip_room_id": str(rid),
            "name": " ".join(name.split()),
            "bed_type": str(bed) if bed else None,
            "max_occupancy": price_entry.get("max_occupancy"),
            "area_sqm": area,
            "price": price_entry.get("price"),
            "currency": price_entry.get("currency") or "VND",
            "tax_included": price_entry.get("tax_included"),
            "raw": {
                "physical_room": room,
                "selected_sale_room_id": price_entry.get("sale_room_id"),
            },
        }
    return rooms


def extract_detail(payloads: list[dict], hotel_id: str, url: str) -> dict:
    """Chuẩn hoá toàn bộ response của một trang detail."""
    images: dict[str, dict] = {}
    amenities: dict[str, dict] = {}
    rooms: dict[str, dict] = {}
    descriptions: list[str] = []
    hotel_types: list[str] = []

    for packet in payloads:
        data = packet.get("response")

        # Ưu tiên join tay physicRoomMap + saleRoomMap trước — heuristic đi
        # theo tên field bên dưới không tự khớp được 2 map này (xem docstring
        # _rooms_from_maps). Payload có thể lồng trong "data" hoặc gốc.
        for candidate in (data, (data or {}).get("data") if isinstance(data, dict) else None):
            if isinstance(candidate, dict) and "physicRoomMap" in candidate:
                rooms.update(_rooms_from_maps(candidate))

        for path, value in _walk(data):
            low_path = path.lower()

            if isinstance(value, str):
                leaf = low_path.rsplit(".", 1)[-1]
                if leaf in {"description", "hoteldescription", "descriptiontext", "introduction"}:
                    text = " ".join(value.split())
                    if len(text) >= 40:
                        descriptions.append(text)
                if leaf in {"hoteltype", "hoteltypename", "categoryname", "propertytype"}:
                    text = " ".join(value.split())
                    if 1 < len(text) < 100:
                        hotel_types.append(text)

            if not isinstance(value, dict):
                continue

            if any(token in low_path for token in ("image", "photo", "picture", "album", "pic")):
                image_url = _image_url(value)
                if image_url:
                    category = _first(value, "category", "categoryName", "typeName", "albumName")
                    images.setdefault(_image_key(image_url), {
                        "url": image_url,
                        "category": str(category) if category else None,
                        "sort_order": len(images),
                    })

            if any(token in low_path for token in ("amenit", "facilit", "service")):
                name = _first(value, "name", "title", "facilityName", "amenityName", "content")
                if isinstance(name, str) and 1 < len(name.strip()) < 160:
                    code = _first(value, "code", "id", "facilityId", "amenityId")
                    category = _first(value, "category", "categoryName", "groupName", "typeName")
                    clean = " ".join(name.split())
                    amenities.setdefault(clean.casefold(), {
                        "code": str(code) if code is not None else None,
                        "name": clean,
                        "category": str(category) if category else None,
                    })

            if "room" in low_path:
                name = _first(value, "roomName", "name", "roomTypeName", "displayName")
                rid = _room_id(value)
                if isinstance(name, str) and (rid or any(k in value for k in ("bedType", "roomArea", "maxOccupancy"))):
                    clean_name = " ".join(name.split())
                    bed = _first(value, "bedType", "bedName", "bedDesc", "bedInfo")
                    if isinstance(bed, dict):
                        bed = _first(bed, "name", "description", "text")
                    room_key = rid or _stable_room_id(clean_name, str(bed) if bed else None)
                    price_obj = _first(value, "priceInfo", "price", "salePrice")
                    currency = _first(value, "currency", "currencyCode")
                    price = None
                    tax_included = None
                    if isinstance(price_obj, dict):
                        price = _number(_first(price_obj, "price", "amount", "salePrice", "totalPrice"))
                        currency = currency or _first(price_obj, "currency", "currencyCode")
                        tax_included = _first(price_obj, "taxIncluded", "isTaxIncluded")
                    else:
                        price = _number(price_obj)
                    rooms.setdefault(room_key, {
                        "trip_room_id": room_key,
                        "name": clean_name,
                        "bed_type": str(bed) if bed else None,
                        "max_occupancy": _integer(_first(value, "maxOccupancy", "maxGuest", "capacity")),
                        "area_sqm": _number(_first(value, "roomArea", "area", "areaSquareMeter")),
                        "price": price,
                        "currency": str(currency or "VND"),
                        "tax_included": bool(tax_included) if tax_included is not None else None,
                        "raw": value,
                    })

    description = max(descriptions, key=len) if descriptions else None
    hotel_type = hotel_types[0] if hotel_types else None
    return {
        "parser_version": PARSER_VERSION,
        "trip_hotel_id": str(hotel_id),
        "url": url,
        "description": description,
        "hotel_type": hotel_type,
        "images": list(images.values()),
        "amenities": list(amenities.values()),
        "rooms": list(rooms.values()),
        "response_count": len(payloads),
    }
