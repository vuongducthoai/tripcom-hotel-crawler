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
from hotel_description import description_text, find_description_info

IMAGE_RE = re.compile(r"^https?://[^\s]+(?:\.(?:jpe?g|png|webp|avif)(?:\?|$)|tripcdn)", re.I)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
IMAGE_VARIANT_RE = re.compile(r"_(?:R|Z)_\d+_\d+[^.]*?(?=\.(?:jpe?g|png|webp|avif)$)", re.I)
PARSER_VERSION = 15

POLICY_HEADINGS = {
    "checkin_checkout": (
        "thời gian nhận và trả phòng", "giờ nhận phòng và trả phòng",
        "check-in and check-out times", "check-in and check-out",
    ),
    "children": ("chính sách cho trẻ em", "child policies", "children policies"),
    "extra_bed": ("nôi/cũi và giường phụ", "cũi và giường phụ", "cribs and extra beds"),
    "breakfast": ("bữa sáng", "breakfast"),
    "pets": ("thú cưng", "pets", "pet policy"),
    "age_requirement": ("giới hạn độ tuổi", "age requirements", "age requirement"),
    "payment": ("thanh toán tại khách sạn", "paying at the hotel", "payment at the hotel"),
    "cancellation": ("chính sách hủy phòng", "cancellation policy"),
}


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


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().casefold()
        if clean in {"true", "1", "yes"}:
            return True
        if clean in {"false", "0", "no"}:
            return False
    return None


def _highlight_value(item: dict) -> bool | None:
    return _boolean(_first(item, "isHighLight", "isHighlight", "highLight", "highlight"))


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


def _add_image(
    images: dict[str, dict], url: str, category_name: str | None = None,
    category_code: Any = None, image_title: str | None = None,
    category_sort: int = 0, source: str = "hotel", fallback: bool = False,
) -> None:
    key = _image_key(url)
    image = images.setdefault(key, {
        "url": url,
        "category": category_name,
        "sort_order": len(images),
        "categories": [],
    })
    if not image.get("category") and category_name:
        image["category"] = category_name
    if category_name:
        if fallback and image["categories"]:
            return
        code = str(category_code) if category_code is not None else category_name.casefold()
        category = {
            "code": code,
            "source": source,
            "name": category_name,
            "image_title": image_title or None,
            "sort_order": category_sort,
        }
        existing = next(
            (item for item in image["categories"] if item.get("code") == code), None
        )
        if existing:
            if not existing.get("image_title") and image_title:
                existing["image_title"] = image_title
            existing["sort_order"] = min(existing.get("sort_order", category_sort), category_sort)
        else:
            image["categories"].append(category)


def _extract_album_images(data: Any, images: dict[str, dict]) -> None:
    """Join ctgethotelalbum parent tabs to their nested image links."""
    for path, value in _walk(data):
        if not isinstance(value, dict) or not isinstance(value.get("imgTabs"), list):
            continue
        for tab_index, tab in enumerate(value["imgTabs"]):
            if not isinstance(tab, dict):
                continue
            category_name = _first(tab, "categoryName", "pictureTypeName", "type")
            if not isinstance(category_name, str) or not category_name.strip():
                continue
            category_name = " ".join(category_name.split())
            raw_code = _first(tab, "categoryId", "pictureTypeId", "typeId", "rank")
            source = "user" if "userprovide" in path.lower() else "hotel"
            category_code = f"{source}:{raw_code if raw_code is not None else category_name.casefold()}"
            image_index = 0
            for _, candidate in _walk(tab.get("imgUrlList") or []):
                if not isinstance(candidate, dict):
                    continue
                link = candidate.get("link")
                if not isinstance(link, str) or not IMAGE_RE.search(link):
                    continue
                _add_image(
                    images, link, category_name, category_code,
                    str(candidate.get("imgTitle") or "").strip() or None,
                    tab_index * 10000 + image_index,
                    source,
                )
                image_index += 1


def _add_amenity(
    amenities: dict[str, dict], name: str, code: Any = None,
    category: str | None = None, category_code: Any = None,
    category_priority: int = 0, free_type: Any = None,
    fee_label: str | None = None, additional_info: Any = None,
    is_highlight: Any = None,
    is_available: Any = None,
) -> None:
    clean = " ".join(name.split())
    if not (1 < len(clean) < 160):
        return
    key = clean.casefold()
    item = amenities.setdefault(key, {
        "code": str(code) if code is not None else None,
        "name": clean,
        "category": category,
        "category_code": str(category_code) if category_code is not None else None,
        "_category_priority": category_priority if category else -1,
        "free_type": _integer(free_type),
        "fee_label": fee_label,
        "additional_info": additional_info or [],
        "is_highlight": _boolean(is_highlight),
        "is_available": _boolean(is_available),
    })
    if item.get("code") is None and code is not None:
        item["code"] = str(code)
    if category and category_priority > item.get("_category_priority", -1):
        item["category"] = category
        item["category_code"] = str(category_code) if category_code is not None else None
        item["_category_priority"] = category_priority
    if item.get("free_type") is None:
        item["free_type"] = _integer(free_type)
    if item.get("is_highlight") is None:
        item["is_highlight"] = _boolean(is_highlight)
    if item.get("is_available") is None:
        item["is_available"] = _boolean(is_available)
    if not item.get("fee_label") and fee_label:
        item["fee_label"] = fee_label
    if not item.get("additional_info") and additional_info:
        item["additional_info"] = additional_info


def _is_room_facility_path(path: str) -> bool:
    return any(token in path.lower() for token in (
        "room", "physicalfacility", "newphysicalfacility", "faciltityinfo",
    ))


def _extract_grouped_amenities(data: Any, amenities: dict[str, dict]) -> None:
    """Join facilityInfo.allFacilities parent groups to their child items."""
    for path, value in _walk(data):
        if "facilit" not in path.lower() or _is_room_facility_path(path) or not isinstance(value, dict):
            continue
        groups = value.get("allFacilities")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                continue
            category = _first(group, "content", "name", "title")
            if not isinstance(category, str) or not category.strip():
                continue
            clean_category = " ".join(category.split())
            category_code = _first(group, "id", "code", "facilityId")
            for child in group["items"]:
                if not isinstance(child, dict):
                    continue
                name = _first(child, "content", "name", "title", "facilityName")
                if not isinstance(name, str):
                    continue
                code = _first(child, "id", "code", "facilityId", "amenityId")
                _add_amenity(
                    amenities, name, code, clean_category, category_code, 100,
                    child.get("freeType"),
                    _first(child, "feeLabel", "chargeDesc", "priceDesc"),
                    child.get("additionInfo"), _highlight_value(child),
                )


def _extract_nearby_places(data: Any) -> list[dict]:
    result: dict[str, dict] = {}
    for _, value in _walk(data):
        if not isinstance(value, dict) or not isinstance(value.get("aroundItemList"), list):
            continue
        for group_index, group in enumerate(value["aroundItemList"]):
            if not isinstance(group, dict):
                continue
            category_code = _first(group, "id", "type")
            category_name = _first(group, "typeName", "name", "title")
            for item_index, poi in enumerate(group.get("poiInfoList") or []):
                if not isinstance(poi, dict) or poi.get("id") is None or not poi.get("name"):
                    continue
                key = str(poi["id"])
                result[key] = {
                    "trip_poi_id": key,
                    "category_code": str(category_code) if category_code is not None else None,
                    "category_name": category_name,
                    "name": poi["name"],
                    "poi_type": _integer(poi.get("poiType")),
                    "latitude": _number(poi.get("lat")),
                    "longitude": _number(poi.get("lng")),
                    "distance_km": _number(poi.get("distance")),
                    "distance_text": poi.get("sinkDistanceText"),
                    "description": poi.get("distanceDescText"),
                    "arrival_type": poi.get("arrivalType"),
                    "tags": poi.get("tagNames") or [],
                    "sort_order": group_index * 1000 + item_index,
                }
    return list(result.values())


def _room_id(obj: dict) -> str | None:
    value = _first(obj, "roomId", "roomID", "roomTypeId", "roomTypeID", "physicalRoomId")
    return str(value) if value is not None else None


def _stable_room_id(name: str, bed: str | None) -> str:
    raw = f"{name}|{bed or ''}".encode("utf-8")
    return "derived:" + hashlib.sha1(raw).hexdigest()[:20]


def _parse_area(value: Any) -> float | None:
    """Normalize a displayed room area to square metres.

    Trip returns metric text for vi-VN and often square feet for en-US.  The
    old parser copied the number unchanged, so ``301 ft²`` became ``301 m²``.
    """
    if isinstance(value, dict):
        value = _first(value, "title", "content", "text", "value")
    number = _number(value)
    if number is None:
        return None
    text = str(value or "").casefold().replace(" ", "")
    if any(unit in text for unit in ("ft²", "ft2", "sqft", "squarefeet", "squarefoot")):
        return round(number * 0.09290304, 1)
    return round(number, 1)


def _info_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value
    elif isinstance(value, dict):
        text = _first(value, "title", "content", "name", "description", "text")
        if isinstance(text, list):
            text = "; ".join(str(item) for item in text if item)
    else:
        text = None
    return " ".join(str(text).split()) if text else None


def _bed_type_text(value: Any) -> str | None:
    """Return the localized bed label from simple or complex Trip room data."""
    direct = _info_text(value)
    if direct:
        return direct
    if not isinstance(value, dict):
        return None
    complex_bed = value.get("complexBed") or value.get("cpxBedInfo") or {}
    if not isinstance(complex_bed, dict):
        return None
    parts: list[str] = []
    for room in complex_bed.get("content") or complex_bed.get("bedDetail") or []:
        if not isinstance(room, dict):
            continue
        details = room.get("detail") or []
        if isinstance(details, str):
            details = [details]
        for detail in details:
            clean = " ".join(str(detail or "").split())
            if clean and clean not in parts:
                parts.append(clean)
    return "; ".join(parts) or None


def _policy_code(text: str) -> str | None:
    folded = " ".join(text.casefold().split()).rstrip(":")
    for code, aliases in POLICY_HEADINGS.items():
        if any(alias in folded for alias in aliases):
            return code
    return None


def _policy_heading_code(text: str) -> str | None:
    folded = " ".join(text.casefold().split()).rstrip(":")
    for code, aliases in POLICY_HEADINGS.items():
        if folded in aliases:
            return code
    return None


def _extract_modal_policies(text: str) -> list[dict]:
    """Parse the visible Policies panel captured by the browser."""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    result: list[dict] = []
    current: dict | None = None
    for line in lines:
        code = _policy_heading_code(line)
        # A section heading is short. Long FAQ/description sentences can also
        # contain words such as "breakfast" and must not start a new section.
        if code and len(line) <= 80:
            if current:
                current["description"] = "\n".join(current.pop("_lines")).strip() or None
                result.append(current)
            current = {
                "code": code,
                "title": line.rstrip(":"),
                "sort_order": len(result),
                "_lines": [],
                "source": "policy_modal",
            }
        elif current:
            current["_lines"].append(line)
    if current:
        current["description"] = "\n".join(current.pop("_lines")).strip() or None
        result.append(current)
    return result


def _extract_faq_policies(data: Any) -> list[dict]:
    if not isinstance(data, dict) or str(data.get("@type") or "").casefold() != "faqpage":
        return []
    result: list[dict] = []
    for item in data.get("mainEntity") or []:
        if not isinstance(item, dict):
            continue
        title = _info_text(item.get("name"))
        answer = item.get("acceptedAnswer") or {}
        description = _info_text(answer.get("text") if isinstance(answer, dict) else answer)
        code = _policy_code(title or "")
        if code and title and description:
            result.append({
                "code": code,
                "title": title,
                "description": description,
                "sort_order": len(result),
                "source": "faq_json_ld",
            })
    return result


def _extract_room_popup_policies(data: dict) -> list[dict]:
    """Use shared child policy from room popups when the hotel panel is absent."""
    room_pop = data.get("roomPopInfo")
    if not isinstance(room_pop, dict):
        return []
    for popup in room_pop.values():
        if not isinstance(popup, dict):
            continue
        child = (popup.get("policyInfo") or {}).get("childPolicy") or {}
        title = _info_text(child.get("title")) if isinstance(child, dict) else None
        texts = child.get("textList") if isinstance(child, dict) else None
        description = "\n".join(str(item).strip() for item in (texts or []) if item).strip()
        if title and description:
            return [{
                "code": "children",
                "title": title,
                "description": description,
                "sort_order": 1,
                "source": "room_popup",
            }]
    return []


def _room_amenity_key(item: dict, name: str) -> str:
    code = _first(item, "id", "code", "facilityId", "amenityId")
    if code is not None:
        return f"code:{code}"
    icon = str(item.get("icon") or "").casefold()
    icon = re.sub(r"^(?:a-|ic_(?:new_)?fa_|ic_new_)", "", icon)
    if icon and icon not in {"checklist", "advantage"}:
        return f"icon:{icon}"
    return "name:" + hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:20]


def _extract_room_amenities(source: dict) -> list[dict]:
    """Extract facilities while preserving their parent group/category."""
    found: dict[str, dict] = {}

    def add(item: dict, category: str | None = None, category_code: Any = None) -> None:
        name = _info_text(item)
        if not name or len(name) > 180:
            return
        key = _room_amenity_key(item, name)
        row = found.setdefault(key, {
            "key": key,
            "code": str(_first(item, "id", "code", "facilityId", "amenityId") or "") or None,
            "name": name,
            "category": category,
            "category_code": str(category_code) if category_code is not None else None,
            "is_highlight": _highlight_value(item),
            "free_type": _integer(item.get("freeType")),
            "additional_info": item.get("additionInfo") or [],
        })
        if not row.get("category") and category:
            row["category"] = category
            row["category_code"] = str(category_code) if category_code is not None else None

    facility = source.get("faciltityInfo") or source.get("facilityInfo") or {}
    for group in facility.get("list") or facility.get("allFacilities") or []:
        if not isinstance(group, dict):
            continue
        category = _info_text(group)
        category_code = _first(group, "id", "code", "facilityId")
        children = group.get("subList") or group.get("items") or []
        for child in children:
            if isinstance(child, dict):
                add(child, category, category_code)

    for container in (source, source.get("roomBasicInfo") or {}):
        for field in ("physicalFacilityList", "newPhysicalFacilityList"):
            for item in container.get(field) or []:
                if isinstance(item, dict):
                    add(item)
    return list(found.values())


def _extract_room_images(source: dict) -> list[dict]:
    found: dict[str, dict] = {}
    for field in ("pictureInfo", "pictureList", "pictures"):
        for _, item in _walk(source.get(field) or []):
            if not isinstance(item, dict):
                continue
            url = _image_url(item)
            if not url:
                continue
            key = _image_key(url)
            found.setdefault(key, {
                "url": url,
                "category_code": str(item.get("categoryId")) if item.get("categoryId") is not None else None,
                "sort_order": len(found),
            })
    return list(found.values())


def _enrich_room(result: dict, source: dict) -> None:
    """Merge physical-room or room-popup fields into one normalized room."""
    basic = source.get("roomBasicInfo") if isinstance(source.get("roomBasicInfo"), dict) else source
    bed_info = basic.get("bedInfo") or source.get("bedInfo") or {}
    house = source.get("houseTypeInfo") or {}

    result["bed_type"] = _bed_type_text(bed_info) or result.get("bed_type")
    result["area_sqm"] = _parse_area(basic.get("areaInfo") or source.get("areaInfo")) or result.get("area_sqm")
    result["view_name"] = (
        _info_text(basic.get("windowInfo"))
        or _info_text(source.get("outdoorLandscapeInfo"))
        or result.get("view_name")
    )
    result["smoking_policy"] = _info_text(basic.get("smokeInfo")) or result.get("smoking_policy")
    result["wifi"] = _info_text(basic.get("wifiInfo")) or result.get("wifi")
    result["floor_label"] = _info_text(basic.get("floorInfo")) or result.get("floor_label")
    result["extra_bed_policy"] = (
        _info_text(bed_info.get("addBed"))
        or _info_text(bed_info.get("addBedContent"))
        or result.get("extra_bed_policy")
    )
    guest_text = _info_text(basic.get("guestInfo"))
    if result.get("max_occupancy") is None and guest_text:
        result["max_occupancy"] = _integer(guest_text)
    for source_key, output_key in (
        ("bedRoomCount", "bedroom_count"),
        ("bathRoomCount", "bathroom_count"),
        ("bedCount", "bed_count"),
    ):
        count = _integer(house.get(source_key))
        if count is not None and count >= 0:
            result[output_key] = count

    images = {_image_key(item["url"]): item for item in result.get("images") or []}
    for item in _extract_room_images(source):
        images.setdefault(_image_key(item["url"]), item)
    result["images"] = list(images.values())

    amenities = {item["key"]: item for item in result.get("amenities") or []}
    for item in _extract_room_amenities(source):
        existing = amenities.get(item["key"])
        if existing:
            if not existing.get("category") and item.get("category"):
                existing.update({"category": item["category"], "category_code": item.get("category_code")})
        else:
            amenities[item["key"]] = item
    result["amenities"] = list(amenities.values())


def _rooms_from_maps(data: dict, default_currency: str = "VND") -> dict[str, dict]:
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
                "currency": price_info.get("currency") or default_currency,
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
        bed = _bed_type_text(room.get("bedInfo"))
        area = _parse_area((room.get("areaInfo") or {}).get("title"))
        price_entry = best.get(str(rid)) or {}
        rooms[str(rid)] = {
            "trip_room_id": str(rid),
            "name": " ".join(name.split()),
            "bed_type": str(bed) if bed else None,
            "max_occupancy": price_entry.get("max_occupancy"),
            "area_sqm": area,
            "price": price_entry.get("price"),
            "currency": price_entry.get("currency") or default_currency,
            "tax_included": price_entry.get("tax_included"),
            "images": [],
            "amenities": [],
            "raw": {
                "physical_room": room,
                "selected_sale_room_id": price_entry.get("sale_room_id"),
            },
        }
        _enrich_room(rooms[str(rid)], room)
    return rooms


def _merge_room_popups(rooms: dict[str, dict], data: dict) -> None:
    room_pop = data.get("roomPopInfo")
    if not isinstance(room_pop, dict):
        return
    for rid, popup in room_pop.items():
        if not isinstance(popup, dict):
            continue
        key = str(popup.get("id") or rid)
        room = rooms.get(key)
        if room is None:
            name = popup.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            room = {
                "trip_room_id": key,
                "name": " ".join(name.split()),
                "bed_type": None,
                "max_occupancy": None,
                "area_sqm": None,
                "price": None,
                "currency": None,
                "tax_included": None,
                "images": [],
                "amenities": [],
                "raw": {},
            }
            rooms[key] = room
        _enrich_room(room, popup)
        room.setdefault("raw", {})["room_popup"] = popup


def extract_detail(
    payloads: list[dict], hotel_id: str, url: str, default_currency: str = "VND",
    locale: str = "vi-VN",
) -> dict:
    """Chuẩn hoá toàn bộ response của một trang detail."""
    images: dict[str, dict] = {}
    amenities: dict[str, dict] = {}
    rooms: dict[str, dict] = {}
    descriptions: list[str] = []
    property_descriptions: list[str] = []
    hotel_types: list[str] = []
    hotel_names: list[str] = []
    hotel_addresses: list[str] = []
    policies: dict[str, dict] = {}
    nearby_places: dict[str, dict] = {}
    is_english = locale.lower().startswith("en")
    room_image_category = "Rooms" if is_english else "Phòng"
    room_amenity_category = "Room amenities" if is_english else "Tiện nghi phòng"
    popular_amenity_category = "Popular amenities" if is_english else "Tiện ích phổ biến"
    other_category = "Other" if is_english else "Khác"

    captured_facilities = next((packet.get("response") for packet in payloads
        if packet.get("url") == "embedded:hotel-facilities"
        and isinstance(packet.get("response"), dict)
        and packet["response"].get("captured")
        and packet["response"].get("items")), None)

    for packet in payloads:
        data = packet.get("response")
        packet_url = str(packet.get("url") or "").lower()
        room_packet = any(token in packet_url for token in (
            "getroom", "roomlist", "roompop", "roomdetail", "room-facilities",
        ))
        if not room_packet:
            info = find_description_info(data, hotel_id)
            if packet.get("url") == "embedded:hotel-description" and isinstance(data, dict):
                if str(data.get("hotel_id")) == str(hotel_id):
                    info = data.get("hotelDescriptionInfo")
            text = description_text(info)
            if text:
                property_descriptions.append(text)

        if packet.get("url") == "embedded:hotel-policies" and isinstance(data, dict):
            for policy in _extract_modal_policies(str(data.get("text") or "")):
                policies[policy["code"]] = policy

        _extract_album_images(data, images)
        if not captured_facilities and not room_packet:
            _extract_grouped_amenities(data, amenities)
        for place in _extract_nearby_places(data):
            nearby_places[place["trip_poi_id"]] = place

        # Ưu tiên join tay physicRoomMap + saleRoomMap trước — heuristic đi
        # theo tên field bên dưới không tự khớp được 2 map này (xem docstring
        # _rooms_from_maps). Payload có thể lồng trong "data" hoặc gốc.
        for candidate in (data, (data or {}).get("data") if isinstance(data, dict) else None):
            if isinstance(candidate, dict) and "physicRoomMap" in candidate:
                rooms.update(_rooms_from_maps(candidate, default_currency))
            if isinstance(candidate, dict) and "roomPopInfo" in candidate:
                _merge_room_popups(rooms, candidate)
                for policy in _extract_room_popup_policies(candidate):
                    policies.setdefault(policy["code"], policy)

        if packet.get("url") == "embedded:json-ld":
            nodes = data if isinstance(data, list) else [data]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                for policy in _extract_faq_policies(node):
                    policies.setdefault(policy["code"], policy)
                node_type = str(node.get("@type") or "").lower()
                if node_type not in {"hotel", "lodgingbusiness", "resort"}:
                    continue
                type_labels = {
                    "hotel": "Hotel" if is_english else "Khách sạn",
                    "resort": "Resort" if is_english else "Khu nghỉ dưỡng",
                    "lodgingbusiness": (
                        "Lodging business" if is_english else "Cơ sở lưu trú"
                    ),
                }
                hotel_types.append(type_labels[node_type])
                if isinstance(node.get("name"), str):
                    hotel_names.append(" ".join(node["name"].split()))
                address = node.get("address")
                if isinstance(address, str):
                    hotel_addresses.append(" ".join(address.split()))
                elif isinstance(address, dict):
                    # Trip's streetAddress commonly already contains locality
                    # and region. Appending them again produces duplicated text.
                    street = address.get("streetAddress")
                    parts = [street] if street else [
                        address.get("addressLocality"), address.get("addressRegion"),
                        address.get("postalCode"), address.get("addressCountry"),
                    ]
                    clean_address = ", ".join(str(part).strip() for part in parts if part)
                    if clean_address:
                        hotel_addresses.append(clean_address)

        for path, value in _walk(data):
            low_path = path.lower()

            if isinstance(value, str):
                leaf = low_path.rsplit(".", 1)[-1]
                property_context = (
                    packet.get("url") == "embedded:page-meta"
                    or (packet.get("url") == "embedded:json-ld"
                        and isinstance(data, dict)
                        and str(data.get("@type") or "").lower() in {"hotel", "lodgingbusiness", "resort"})
                    or leaf in {"hoteldescription", "introduction"}
                )
                unrelated_context = room_packet or any(token in low_path for token in (
                    "room", "policy", "faq", "comment", "review", "rating", "hotelList".lower(),
                ))
                if property_context and not unrelated_context and leaf in {"description", "hoteldescription", "descriptiontext", "introduction"}:
                    text = " ".join(value.split())
                    generic_markers = (
                        "bạn đang tìm đặt phòng",
                        "hãy chọn phòng cho bạn",
                        "so sánh giá cả và đặt",
                        "chúng tôi khuyên bạn nên đặt",
                        "phải thanh toán thêm",
                        "looking to book",
                        "select rooms",
                        "compare prices and book",
                        "compare the latest room rates",
                    )
                    if len(text) >= 40 and not any(
                        marker in text.casefold() for marker in generic_markers
                    ):
                        descriptions.append(text)
                if leaf in {"hoteltype", "hoteltypename", "accommodationtype"}:
                    text = " ".join(value.split())
                    if 1 < len(text) < 100:
                        hotel_types.append(text)

            if not isinstance(value, dict):
                continue

            if any(token in low_path for token in ("image", "photo", "picture", "album", "pic")):
                image_url = _image_url(value)
                if image_url:
                    category = _first(value, "category", "categoryName", "typeName", "albumName")
                    if category:
                        _add_image(images, image_url, str(category))
                    else:
                        is_room_image = any(
                            token in low_path
                            for token in ("room", "physicroommap", "pictureinfo")
                        )
                        derived_name = room_image_category if is_room_image else other_category
                        derived_code = "derived:room" if is_room_image else "derived:other"
                        derived_source = (
                            "user" if any(token in low_path for token in ("user", "comment"))
                            else "hotel"
                        )
                        _add_image(
                            images, image_url, derived_name, derived_code,
                            category_sort=len(images), source=derived_source,
                            fallback=True,
                        )

            if (not captured_facilities and not room_packet and not _is_room_facility_path(low_path)
                    and any(token in low_path for token in ("amenit", "facilit", "service"))):
                name = _first(value, "name", "title", "facilityName", "amenityName", "content")
                # A dict with `items` is a facility group; its content is the
                # parent category, not another amenity row.
                if isinstance(name, str) and not isinstance(value.get("items"), list):
                    code = _first(value, "code", "id", "facilityId", "amenityId")
                    category = _first(value, "category", "categoryName", "groupName", "typeName")
                    category_code = None
                    category_priority = 90 if category else 0
                    if not category:
                        if "allpopularfacility" in low_path:
                            category = popular_amenity_category
                            category_code = "derived:popular"
                            category_priority = 50
                        elif any(token in low_path for token in (
                            "room", "physicroommap", "physicalfacility",
                            "newphysicalfacility", "faciltityinfo",
                        )):
                            category = room_amenity_category
                            category_code = "derived:room"
                            category_priority = 40
                        else:
                            category = other_category
                            category_code = "derived:other"
                            category_priority = 1
                    _add_amenity(
                        amenities, name, code,
                        str(category) if category else None,
                        category_code, category_priority, value.get("freeType"),
                        _first(value, "feeLabel", "chargeDesc", "priceDesc"),
                        value.get("additionInfo"), _highlight_value(value),
                    )

            if "room" in low_path:
                name = _first(value, "roomName", "name", "roomTypeName", "displayName")
                rid = _room_id(value)
                if isinstance(name, str) and (rid or any(k in value for k in ("bedType", "roomArea", "maxOccupancy"))):
                    clean_name = " ".join(name.split())
                    bed = _first(value, "bedType", "bedName", "bedDesc", "bedInfo")
                    bed = _bed_type_text(bed) if isinstance(bed, dict) else bed
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
                        "area_sqm": _parse_area(_first(value, "roomArea", "area", "areaSquareMeter")),
                        "price": price,
                        "currency": str(currency or default_currency),
                        "tax_included": bool(tax_included) if tax_included is not None else None,
                        "images": [],
                        "amenities": [],
                        "raw": value,
                    })

    # A popup response can arrive before the room-list response. Re-run only
    # the cheap room-popup merge after every physical room has been collected.
    for packet in payloads:
        data = packet.get("response")
        for candidate in (data, (data or {}).get("data") if isinstance(data, dict) else None):
            if isinstance(candidate, dict):
                _merge_room_popups(rooms, candidate)

    if captured_facilities:
        amenities.clear()
        for item in captured_facilities["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            _add_amenity(amenities, item["name"], code=item.get("code"),
                         category=item.get("category") or other_category,
                         category_code=item.get("category_code"),
                         category_priority=100 if item.get("category") else 1,
                         fee_label=item.get("fee_label"), additional_info=item.get("additional_info"),
                         is_highlight=item.get("is_highlight"), is_available=item.get("is_available"))
            key = " ".join(item["name"].split()).casefold()
            if item.get("is_highlight") and key in amenities:
                amenities[key]["is_highlight"] = True

    description = (max(property_descriptions, key=len) if property_descriptions
                   else max(descriptions, key=len) if descriptions else None)
    hotel_type = hotel_types[0] if hotel_types else None
    return {
        "parser_version": PARSER_VERSION,
        "hotel_amenities_captured": bool(captured_facilities),
        "trip_hotel_id": str(hotel_id),
        "url": url,
        "name": hotel_names[0] if hotel_names else None,
        "address": hotel_addresses[0] if hotel_addresses else None,
        "description": description,
        "hotel_type": hotel_type,
        "images": list(images.values()),
        "amenities": [
            {k: v for k, v in item.items() if not k.startswith("_")}
            for item in amenities.values()
        ],
        "policies": list(policies.values()),
        "nearby_places": list(nearby_places.values()),
        "rooms": list(rooms.values()),
        "response_count": len(payloads),
    }
