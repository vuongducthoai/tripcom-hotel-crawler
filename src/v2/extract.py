"""Bóc tách raw Trip.com → "bundle" v2 của một khách sạn, kèm kiểm tra lớp 1–3.

    bundle, issues = build_bundle(dump, locale="vi-VN", currency="VND", raw_path=...)

- Lớp 1 (response): có phải dữ liệu thật không — bị chặn, sai ngôn ngữ, sai tiền tệ.
- Lớp 2 (bản ghi): mỗi dòng đi qua model Pydantic (models.py); dòng sai bị bỏ,
  có ghi lý do. Các chỗ chuẩn hóa (9999 → NULL, ft² → m²…) được đếm lại.
- Lớp 3 (liên kết): gói giá phải thuộc loại phòng có thật, không trùng khóa…

Chỉ ĐỌC dump, không đụng DB. bundle = None nghĩa là cả khách sạn bị loại.

Nguồn dữ liệu:
  * raw hiện có (responses các API + normalized của crawler)
  * khối hotelDetailResponse của trang (sao, tọa độ, chính sách có cấu trúc…):
    lấy từ dump["hotelDetailResponse"], hoặc response "embedded:hotel-detail-response",
    hoặc truyền vào qua tham số `detail` (vd đọc từ output/probe_v2). Raw cũ chưa
    có khối này → cảnh báo no_detail_block, bỏ qua các phần phụ thuộc.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

from . import models as M
from .rules import ERROR, Issues

# Bóc tách thay đổi → tăng số này (ghi vào hotel_crawls.parser_version)
PARSER_VERSION = 1

ROOM_API = "getHotelRoomListOversea"
POP_API = "getHotelRoomPopInfoPCOnline"
ALBUM_API = "ctgethotelalbum"
COMMENT_API = "getHotelCommentInfo"
NEARBY_API = "ctGetNearbyPlaceInfo"
ADDITIONAL_API = "getDetailAdditionalInfo"
KEY_APIS = (ROOM_API, POP_API, ALBUM_API, COMMENT_API, NEARBY_API, ADDITIONAL_API)

VI_CHARS = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.I)
MARKUP = re.compile(r"\{/?\d+\}")
PROMO = re.compile(r"^(book your stay|looking to book|bạn đang tìm đặt phòng|đặt phòng .{0,80} ngay)", re.I)
LOCAL_NAME_PREFIX = re.compile(r"^[^:]{3,40}:\s*")
IMAGE_SIZED = re.compile(r"_(?:R|Z|W|C)_\d+_\d+[^/]*?(\.(?:jpe?g|png|webp))(?:\?.*)?$", re.I)
FT2_TO_M2 = Decimal("0.09290304")

# Khoảng giá "bình thường" cho một đêm — ngoài khoảng thì chỉ cảnh báo.
PRICE_RANGE = {"VND": (50_000, 500_000_000), "USD": (2, 50_000)}


def short_locale(locale: str) -> str:
    """'vi-VN' → 'vi' (khóa i18n trong bảng)."""
    return locale.split("-")[0].lower()


# ============================================================== bundle
@dataclass
class Bundle:
    trip_hotel_id: str
    locale: str                 # 'vi' / 'en'
    raw_locale: str             # 'vi-VN'
    currency: str
    crawled_at: datetime | None
    raw_path: str | None
    has_detail: bool = False
    detail_text: dict = field(default_factory=dict)     # chữ lấy từ hotelDetailResponse
    rooms_sold_out: bool = False
    country: M.Country | None = None
    city: M.City | None = None
    hotel: M.Hotel | None = None
    hotel_i18n: M.HotelI18n | None = None
    highlights: list[M.HotelHighlight] = field(default_factory=list)
    image_categories: list[M.ImageCategory] = field(default_factory=list)
    images: list[M.HotelImage] = field(default_factory=list)
    amenities: list[M.Amenity] = field(default_factory=list)            # danh mục
    hotel_amenities: list[M.HotelAmenity] = field(default_factory=list)
    policy_sections: list[M.PolicySection] = field(default_factory=list)
    policy: M.HotelPolicy | None = None
    rooms: list[M.RoomType] = field(default_factory=list)
    room_i18n: list[M.RoomTypeI18n] = field(default_factory=list)
    room_images: list[M.RoomImage] = field(default_factory=list)
    room_amenities: list[M.RoomAmenity] = field(default_factory=list)
    offers: list[M.RoomOffer] = field(default_factory=list)
    offer_prices: list[M.RoomOfferPrice] = field(default_factory=list)
    offer_i18n: list[M.RoomOfferI18n] = field(default_factory=list)
    snapshots: list[M.PriceSnapshot] = field(default_factory=list)
    review: M.ReviewSummary | None = None
    review_tags: list[M.ReviewTag] = field(default_factory=list)
    nearby: list[M.NearbyPlace] = field(default_factory=list)

    def sections(self) -> dict[str, bool]:
        """Mục nào có dữ liệu — ghi vào hotel_crawls.sections và tính tỷ lệ cả đợt."""
        return {
            "detail": self.has_detail, "description": bool(self.hotel_i18n and self.hotel_i18n.description),
            "images": bool(self.images), "amenities": bool(self.hotel_amenities),
            "policies": bool(self.policy_sections), "rooms": bool(self.rooms),
            "offers": bool(self.offers), "sold_out": self.rooms_sold_out,
            "reviews": bool(self.review), "nearby": bool(self.nearby),
        }


# ============================================================== tiện ích
def api(dump: dict, name: str) -> Any:
    for packet in dump.get("responses") or []:
        if name in str(packet.get("url") or ""):
            return packet.get("response")
    return None


def api_data(dump: dict, name: str) -> dict:
    value = api(dump, name)
    return (value.get("data") or {}) if isinstance(value, dict) else {}


ANTIBOT_XOR_KEY = 0x0A


def _decode_obfuscated(value: list) -> str | None:
    """Giải mảng byte XOR 0x0A của Trip.com; None nếu không phải thông báo chặn."""
    if not (50 <= len(value) <= 20_000):
        return None
    if not all(isinstance(byte, int) and 0 <= byte <= 255 for byte in value):
        return None
    text = "".join(chr(byte ^ ANTIBOT_XOR_KEY) for byte in value)
    return text if ("failedcause" in text or "Antibot" in text) else None


def blocked_reason(value: Any) -> str | None:
    """Cùng logic crawl_detail._blocked_reason (chép lại để khỏi import playwright):
    mã htlSpiderActionErrorCode (vd 4030), hoặc mảng byte XOR có 'failedcause'."""
    if isinstance(value, dict):
        if value.get("htlSpiderActionErrorCode") is not None:
            return f"htlSpiderActionErrorCode={value['htlSpiderActionErrorCode']}"
        for child in value.values():
            found = blocked_reason(child)
            if found:
                return found
    elif isinstance(value, list):
        if _decode_obfuscated(value):
            return "Antibot"
        for child in value:
            if isinstance(child, (dict, list)):
                found = blocked_reason(child)
                if found:
                    return found
    return None


class Cleaner:
    """Chuẩn hóa chữ/số và đếm số lần tự sửa vào Issues."""

    def __init__(self, issues: Issues, locale: str):
        self.issues = issues
        self.locale = locale

    def text(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value if v not in (None, ""))
        raw = str(value)
        out = raw
        if MARKUP.search(out):
            out = MARKUP.sub("", out)
            self.issues.fix("fix_markup")
        out = "\n".join(re.sub(r"[ \t ]+", " ", line).strip() for line in out.splitlines())
        out = re.sub(r"\n{3,}", "\n\n", out).strip()
        if out != raw and not MARKUP.search(raw):
            self.issues.fix("fix_whitespace")
        return out or None

    def count(self, value: Any) -> int | None:
        """-1 của Trip.com = 'không rõ' → NULL."""
        if value in (None, ""):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number < 0:
            self.issues.fix("fix_negative_count")
            return None
        return number

    def number(self, text: str) -> Decimal | None:
        """'1.076' (vi) / '1,076' (en) / '22.5' → Decimal."""
        text = text.strip()
        if self.locale == "vi":
            if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
                text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            return Decimal(text)
        except InvalidOperation:
            return None

    def area(self, text: str | None, key) -> tuple[Decimal | None, Decimal | None]:
        """'236–322 ft²' → (21.93, 29.91) m²; '22 m²' / '22㎡' → (22, None)."""
        if not text:
            return None, None
        numbers = [self.number(n) for n in re.findall(r"\d+(?:[.,]\d+)*", text)]
        numbers = [n for n in numbers if n is not None]
        lowered = text.lower()
        if not numbers:
            self.issues.add("area_unparsed", "room", key=key, field="area", value=text)
            return None, None
        if "ft" in lowered:
            factor = FT2_TO_M2
            self.issues.fix("fix_area_unit")
        elif any(u in lowered for u in ("m²", "㎡", "m2", "sqm", " m")):
            factor = Decimal(1)
        else:
            self.issues.add("area_unparsed", "room", key=key, field="area", value=text,
                            detail="không rõ đơn vị")
            return None, None
        low = (numbers[0] * factor).quantize(Decimal("0.01"))
        high = (numbers[1] * factor).quantize(Decimal("0.01")) if len(numbers) > 1 else None
        if high is not None:
            self.issues.fix("fix_area_range")
            if high < low:
                low, high = high, low
        if not (Decimal(3) <= low <= Decimal(2000)) or (high is not None and high > 5000):
            self.issues.add("area_implausible", "room", key=key, field="area", value=text)
            return None, None
        return low, high

    def coords(self, lat, lng, entity: str, key) -> tuple[float | None, float | None]:
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            return None, None
        if lat_f == 0 and lng_f == 0:
            self.issues.fix("fix_zero_coords")
            return None, None
        if not (-90 <= lat_f <= 90 and -180 <= lng_f <= 180):
            self.issues.add("coords_invalid", entity, key=key, field="lat,lng", value=f"{lat},{lng}")
            return None, None
        return lat_f, lng_f

    def image_url(self, url: str | None) -> str | None:
        if not url:
            return None
        url = url.strip()
        if url.startswith("//"):
            url = "https:" + url
        original = IMAGE_SIZED.sub(r"\1", url)
        if original != url:
            self.issues.fix("fix_image_original")
        return original


def first_int(text: Any) -> int | None:
    found = re.search(r"\d+", str(text or ""))
    return int(found.group()) if found else None


def parse_ms_date(value: Any) -> datetime | None:
    """'/Date(1789923600000+0800)/' → datetime UTC."""
    found = re.search(r"/Date\((-?\d+)", str(value or ""))
    return datetime.fromtimestamp(int(found.group(1)) / 1000, tz=timezone.utc) if found else None


def parse_iso(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def to_decimal(value: Any) -> Decimal | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def validated(model: type[BaseModel], data: dict, issues: Issues, entity: str, key=None):
    """Lớp 2: đưa một dòng qua model; sai → ghi lỗi và trả None (bỏ dòng)."""
    try:
        return model(**data)
    except ValidationError as exc:
        for err in exc.errors():
            where = ".".join(str(p) for p in err.get("loc") or ()) or None
            issues.add("record_invalid", entity, key=key, field=where,
                       value=err.get("input") if not isinstance(err.get("input"), dict) else None,
                       detail=err.get("msg"))
        return None


# ============================================================== lớp 1
def check_response(dump: dict, raw_locale: str, currency: str, file_hotel_id: str | None,
                   issues: Issues) -> str | None:
    """Trả về trip_hotel_id nếu raw dùng được; None nếu phải bỏ cả khách sạn."""
    normalized = dump.get("normalized") or {}
    target = dump.get("target") or {}
    hotel_id = str(normalized.get("trip_hotel_id") or target.get("trip_hotel_id") or file_hotel_id or "")
    if not hotel_id.isdigit():
        issues.add("missing_hotel_id", "hotel", value=hotel_id)
        return None
    if file_hotel_id and file_hotel_id != hotel_id:
        issues.add("hotel_id_mismatch", "hotel", value=f"file={file_hotel_id} raw={hotel_id}")
        return None

    reason = normalized.get("blocked")
    if not reason:
        for name in KEY_APIS:
            reason = blocked_reason(api(dump, name))
            if reason:
                break
    if reason:
        issues.add("blocked", "hotel", value=reason)
        return None
    if normalized.get("page_dead"):
        issues.add("page_dead", "hotel", value=normalized.get("page_dead"))
        return None

    # Ngôn ngữ: thông số trong raw + chữ giao diện có đúng thứ tiếng không
    lang = short_locale(raw_locale)
    declared = str(normalized.get("locale") or "")
    url = str(dump.get("url") or normalized.get("url") or "")
    url_locale = re.search(r"[?&]locale=([A-Za-z-]+)", url)
    if (declared and declared != raw_locale) or (url_locale and url_locale.group(1) != raw_locale):
        issues.add("wrong_locale", "hotel", value=declared or url_locale.group(1),
                   detail=f"cần {raw_locale}")
        return None
    ui = list(ui_strings(dump))
    if len(ui) >= 5:
        ratio = sum(1 for s in ui if VI_CHARS.search(s)) / len(ui)
        if (lang == "vi" and ratio < 0.2) or (lang == "en" and ratio > 0.2):
            issues.add("wrong_locale", "hotel", value=f"{ratio:.0%} chữ giao diện có dấu tiếng Việt",
                       detail=f"cần {raw_locale}")
            return None

    # Tiền tệ: giá trong danh sách phòng
    currencies = {((s.get("priceInfo") or {}).get("currency"))
                  for s in (api_data(dump, ROOM_API).get("saleRoomMap") or {}).values()} - {None, ""}
    if currencies and currencies != {currency}:
        issues.add("wrong_currency", "hotel", value=",".join(sorted(currencies)), detail=f"cần {currency}")
        return None
    return hotel_id


def ui_strings(dump: dict) -> Iterable[str]:
    """Chữ do Trip.com dịch theo ngôn ngữ (không lấy tên riêng như tên khách sạn)."""
    for sale in list((api_data(dump, ROOM_API).get("saleRoomMap") or {}).values())[:20]:
        for part in ("titleInfo", "cancelInfo", "paymentInfo", "confirmInfo"):
            title = (sale.get(part) or {}).get("title") or (sale.get(part) or {}).get("paymentTitleNew")
            if title:
                yield str(title)
    album = api_data(dump, ALBUM_API).get("hotelImagePop") or {}
    for tab in ((album.get("hotelProvide") or {}).get("imgTabs") or []):
        if tab.get("categoryName"):
            yield str(tab["categoryName"])
    for group in (api_data(dump, NEARBY_API).get("placeInfoList") or []):
        if group.get("name"):
            yield str(group["name"])
        for place in (group.get("places") or [])[:3]:
            if place.get("distanceDesc"):
                yield str(place["distanceDesc"])
    rating = api_data(dump, COMMENT_API).get("commentRating") or {}
    for key in ("ratingLocationShowItem", "ratingFacilityShowItem", "ratingServiceShowItem"):
        if rating.get(key):
            yield str(rating[key])


# ============================================================== khối hotelDetailResponse
def find_detail(dump: dict) -> dict | None:
    if isinstance(dump.get("hotelDetailResponse"), dict):
        return dump["hotelDetailResponse"]
    value = api(dump, "embedded:hotel-detail-response")
    return value if isinstance(value, dict) else None


def extract_detail(b: Bundle, detail: dict, c: Cleaner, issues: Issues) -> None:
    base = detail.get("hotelBaseInfo") or {}
    position = detail.get("hotelPositionInfo") or {}
    star = base.get("starInfo") or {}
    medal = base.get("medalInfo") or {}
    policy = detail.get("hotelPolicyInfo") or {}

    if base.get("countryId"):
        b.country = validated(M.Country, {"trip_country_id": base["countryId"],
                                          "name": c.text(base.get("countryName"))}, issues, "country")
        if base.get("cityId"):
            b.city = validated(M.City, {
                "trip_city_id": base["cityId"], "trip_country_id": base["countryId"],
                "trip_province_id": base.get("provinceId") or None,
                "utc_offset_sec": base.get("timeOffset"),
                "name": c.text(base.get("cityName")), "province_name": c.text(base.get("provinceName")),
            }, issues, "city")

    lat, lng = c.coords(position.get("lat"), position.get("lng"), "hotel", b.trip_hotel_id)
    open_year, reno_year = first_int(base.get("openYear")), first_int(base.get("fitmentYear"))
    if open_year and reno_year and reno_year < open_year:
        issues.add("renovated_before_open", "hotel", field="renovated_year",
                   value=f"{open_year}/{reno_year}")
    # 'star' = hạng sao chính thức; 'diamond'/'circle' = Trip.com tự xếp hạng
    star_type = star.get("type") if star.get("type") in ("star", "diamond", "circle") else None
    hotel = validated(M.Hotel, {
        "trip_hotel_id": int(b.trip_hotel_id),
        "trip_city_id": base.get("cityId") or None,
        "star_level": star.get("level") if isinstance(star.get("level"), (int, float)) else None,
        "star_type": star_type, "is_super_star": bool(star.get("superStar")) if "superStar" in star else None,
        "medal_type": medal.get("type"), "open_year": open_year, "renovated_year": reno_year,
        "latitude": lat, "longitude": lng,
        "is_private_host": bool(policy.get("privateHostInfo")),
    }, issues, "hotel", b.trip_hotel_id)
    if hotel:
        b.hotel = hotel

    name_info = base.get("nameInfo") or {}
    local = c.text(name_info.get("localNameTip"))
    if local and LOCAL_NAME_PREFIX.match(local):
        local = LOCAL_NAME_PREFIX.sub("", local)
        issues.fix("fix_local_name_prefix")
    if local and local == c.text(name_info.get("name")):
        local = None     # tên địa phương trùng tên hiển thị → không lưu lặp
    desc_info = detail.get("hotelDescriptionInfo") or {}
    description = c.text(desc_info.get("description")) or c.text(
        "\n\n".join(s.get("desc") or "" for s in desc_info.get("sectionList") or []))
    b.detail_text = {  # dùng lại ở extract_hotel_i18n
        "name": c.text(name_info.get("name")), "local_name": local,
        "address": c.text(position.get("address")), "zone_name": c.text(position.get("zoneName")),
        "traffic_desc": c.text((position.get("trafficInfo") or {}).get("trafficDesc")),
        "description": description, "last_booked_text": c.text(base.get("lastBooking")),
    }

    highlight_list = ((base.get("newHighlights") or {}).get("list")
                      or (base.get("highlights") or {}).get("list") or [])
    for order, item in enumerate(highlight_list):
        row = validated(M.HotelHighlight, {
            "locale": b.locale, "sort_order": order,
            "trip_tag_id": str(item.get("tagId") or "") or None,
            "title": c.text(item.get("tagTitle") or item.get("title")),
            "description": c.text(item.get("desc")),
            "icon_url": item.get("icon") or item.get("iconUrl") or None,
        }, issues, "highlight", order)
        if row:
            b.highlights.append(row)

    order = 0
    for code, section in policy.items():
        if not isinstance(section, dict) or not section.get("title") or not section.get("content"):
            continue
        lines = [(c.text(line.get("title")), c.text(line.get("description")))
                 for line in section["content"] if isinstance(line, dict)]
        lines = [(label.rstrip(":").strip() if label else None, text) for label, text in lines if text]
        if not lines:
            continue
        row = validated(M.PolicySection, {"locale": b.locale, "section_code": code, "sort_order": order,
                                          "title": c.text(section["title"]), "lines": lines},
                        issues, "policy", code)
        if row:
            b.policy_sections.append(row)
            order += 1
    credit = policy.get("credit") or {}
    if credit.get("title"):
        methods = [c.text(credit.get("cashDesc"))] if credit.get("cashDesc") else []
        row = validated(M.PolicySection, {
            "locale": b.locale, "section_code": "credit", "sort_order": order,
            "title": c.text(credit["title"]),
            "lines": [(None, m) for m in methods] or [(None, c.text(credit["title"]))],
        }, issues, "policy", "credit")
        if row:
            b.policy_sections.append(row)
            order += 1
    if b.policy_sections:
        b.policy = normalize_policy(b.policy_sections, issues, b.locale)


# Chuẩn hóa chính sách đọc được cả bản Anh lẫn bản Việt (bản Anh được ưu tiên
# khi ghi DB — xem writer._write_policies). Thứ tự kiểm tra quan trọng:
# "không được phép" chứa "được phép", "allowed upon request" chứa "allowed".
NO = r"not allowed|cannot|can't|unavailable|not accepted|not available|no (pets|children)|không được phép|không cho phép|không chấp nhận|không nhận|không thể"
ON_REQUEST = r"on request|upon request|contact the (hotel|property)|nếu có yêu cầu|theo yêu cầu|liên hệ"
YES = r"allowed|welcome|accepted|available|được phép|chào đón|chấp nhận|có thể"


def _state(text: str) -> str:
    if not text:
        return "unknown"
    if re.search(NO, text, re.I):
        return "no"
    if re.search(ON_REQUEST, text, re.I):
        return "on_request"
    if re.search(YES, text, re.I):
        return "yes"
    return "unknown"


def _time(text: str | None) -> time | None:
    found = re.search(r"(\d{1,2}):(\d{2})", text or "")
    if not found or int(found.group(1)) > 23:
        return None
    return time(int(found.group(1)), int(found.group(2)))


def _int(pattern: str, text: str) -> int | None:
    found = re.search(pattern, text, re.I)
    if not found:
        return None
    value = next((g for g in found.groups() if g), None)
    return int(value) if value else None


def normalize_policy(sections: list[M.PolicySection], issues: Issues, lang: str) -> M.HotelPolicy | None:
    """Giá trị chuẩn hóa để lọc (giờ nhận/trả phòng, thú cưng, trẻ em…), từ bản 'en' hoặc 'vi'."""
    by_code = {s.section_code: s for s in sections}

    def text(*codes: str) -> str:
        return " | ".join(t for code in codes for _, t in (by_code[code].lines if code in by_code else []))

    def labelled(code: str, *prefixes: str) -> str | None:
        for label, value in (by_code[code].lines if code in by_code else []):
            if any((label or "").lower().startswith(p) for p in prefixes):
                return value
        return None

    checkin = labelled("checkInAndOut", "check-in", "nhận phòng")
    checkin_range = re.findall(r"\d{1,2}:\d{2}", checkin or "")
    hours = text("checkInAndOut")
    child = text("childPolicy")
    beds = text("cribAndExtraBed")
    breakfast = text("breakfast")
    deposit = text("deposit")
    quiet = re.findall(r"\d{1,2}:\d{2}", text("quiteTime", "quietTime"))   # Trip.com viết "quiteTime"
    credit = text("credit").lower()
    bed_state = _state(beds) if re.search(NO, beds, re.I) else None   # "tùy loại phòng" → không kết luận
    data = {
        "checkin_from": _time(checkin_range[0]) if checkin_range else None,
        "checkin_until": _time(checkin_range[1]) if len(checkin_range) > 1 else None,
        "checkout_until": _time(labelled("checkInAndOut", "check-out", "trả phòng")),
        "front_desk_24h": bool(re.search(r"24/7|24 hours|24 giờ", hours, re.I)) or None,
        "min_checkin_age": _int(r"at least (\d+)|(?:ít nhất|tối thiểu|từ đủ|đủ) (\d+) tuổi", text("ageLimit")),
        "children_allowed": _state(child) if "childPolicy" in by_code else None,
        "child_min_age": _int(r"(\d+) years? old and (?:above|older)|aged (\d+) and (?:above|older)"
                              r"|(\d+) tuổi trở lên", child),
        "child_free_max_age": _int(r"(?:between )?0\s*(?:and|-|–|to|đến)\s*(\d+)", child),
        "extra_bed": bed_state, "crib": bed_state,
        "breakfast_available": (not re.search(r"not (provided|available)|no breakfast|không có bữa sáng"
                                              r"|không phục vụ bữa sáng", breakfast, re.I)) if breakfast else None,
        "deposit_required": (not re.search(r"no deposit|không (yêu cầu|cần) đặt cọc", deposit, re.I))
                            if deposit else None,
        "pets": _state(text("pet")) if "pet" in by_code else None,
        "service_animals": _state(text("serviceAnimal")) if "serviceAnimal" in by_code else None,
        "quiet_hours_from": _time(quiet[0]) if len(quiet) > 1 else None,
        "quiet_hours_until": _time(quiet[1]) if len(quiet) > 1 else None,
        "payment_methods": ["cash"] if ("cash" in credit or "tiền mặt" in credit) else None,
        "parsed_from_locale": lang,
    }
    return validated(M.HotelPolicy, data, issues, "policy", "normalized")


# ============================================================== phần có trong raw hiện tại
def extract_hotel_i18n(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    normalized = dump.get("normalized") or {}
    target = dump.get("target") or {}
    from_detail = b.detail_text
    name = from_detail.get("name") or c.text(normalized.get("name")) or c.text(target.get("name"))
    if not name:
        issues.add("missing_name", "hotel")
        return
    description, source = from_detail.get("description"), "intro" if from_detail.get("description") else None
    if not description and normalized.get("description"):
        description, source = c.text(normalized["description"]), "meta"
    if description and PROMO.search(description):
        issues.add("promo_description", "hotel", field="description", value=description[:120])
        description, source = None, None
    if not description:
        issues.add("no_description", "hotel")
    row = validated(M.HotelI18n, {
        "locale": b.locale, "name": name, "local_name": from_detail.get("local_name"),
        "address": from_detail.get("address") or c.text(normalized.get("address")),
        "zone_name": from_detail.get("zone_name"), "traffic_desc": from_detail.get("traffic_desc"),
        "hotel_type": c.text(normalized.get("hotel_type")),
        "description": description, "description_source": source,
        "last_booked_text": from_detail.get("last_booked_text"),
    }, issues, "hotel_i18n", b.trip_hotel_id)
    if row is None:
        issues.add("missing_name", "hotel", detail="bản ghi tên/địa chỉ không hợp lệ")
        return
    b.hotel_i18n = row
    url = str(dump.get("url") or normalized.get("url") or target.get("url") or "") or None
    if b.hotel is None:  # raw cũ: chỉ có mã khách sạn
        b.hotel = M.Hotel(trip_hotel_id=int(b.trip_hotel_id), detail_url=url)
    elif not b.hotel.detail_url:
        b.hotel = b.hotel.model_copy(update={"detail_url": url})


def extract_images(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    data = api_data(dump, ALBUM_API)
    pop = data.get("hotelImagePop") or {}
    cover = {c.image_url(i.get("imgUrl")) for i in ((data.get("hotelTopImage") or {}).get("imgUrlList") or [])}
    seen: set[str] = set()
    order = 0

    def add(url, uploader, category, picture_id):
        nonlocal order
        url = c.image_url(url)
        if not url:
            return
        if url in seen:
            issues.fix("fix_image_duplicate")
            return
        row = validated(M.HotelImage, {"url": url, "uploader": uploader, "trip_category_id": category,
                                       "trip_picture_id": picture_id, "sort_order": order,
                                       "is_cover": url in cover}, issues, "image", url)
        if row is None:
            issues.add("bad_url", "image", key=url, value=url)
            return
        seen.add(url)
        b.images.append(row)
        order += 1

    categories: dict[int, str] = {}
    for tab in ((pop.get("hotelProvide") or {}).get("imgTabs") or []):
        cat = tab.get("categoryId")
        if cat is not None and tab.get("categoryName"):
            categories.setdefault(int(cat), c.text(tab["categoryName"]))
        for group in tab.get("imgUrlList") or []:
            for img in group.get("subImgUrlList") or []:
                add(img.get("link"), "hotel", img.get("categoryId", cat), img.get("pictureId"))
    for tab in ((pop.get("userProvide") or {}).get("imgTabs") or []):
        cat = tab.get("categoryId")
        if cat is not None and tab.get("categoryName"):
            categories.setdefault(int(cat), c.text(tab["categoryName"]))
        for img in tab.get("subUserAlbumCommentInfo") or []:
            add(img.get("picture"), "guest", img.get("categoryId", cat), None)
    if not b.images:  # album không có → ảnh crawler đã chuẩn hóa
        for img in (dump.get("normalized") or {}).get("images") or []:
            add(img.get("url"), "hotel", None, None)
    for cat, name in categories.items():
        if name:
            b.image_categories.append(M.ImageCategory(trip_category_id=cat, locale=b.locale, name=name))
    known = set(categories)
    for img in b.images:  # ảnh thuộc tab không có tên → không gán tab (tránh khóa ngoại hỏng)
        if img.trip_category_id is not None and img.trip_category_id not in known:
            img.trip_category_id = None
    if not b.images:
        issues.add("no_images", "hotel")


def extract_amenities(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    items = (dump.get("normalized") or {}).get("amenities") or []
    if not items:
        embedded = api(dump, "embedded:hotel-facilities")
        items = (embedded or {}).get("items") or [] if isinstance(embedded, dict) else []
    seen: set[int] = set()
    no_code: list[str] = []
    for item in items:
        code = str(item.get("code") or "")
        if not code.isdigit():   # vd "Tiếng Việt" (ngôn ngữ phục vụ) không có mã tiện nghi
            no_code.append(str(item.get("name") or "?"))
            continue
        code_int = int(code)
        if code_int in seen:
            continue
        category = str(item.get("category_code") or "")
        category_id = int(category) if category.lstrip("-").isdigit() else None
        catalog = validated(M.Amenity, {"trip_amenity_id": code_int, "trip_category_id": category_id,
                                        "locale": b.locale, "name": c.text(item.get("name")),
                                        "category_name": c.text(item.get("category"))},
                            issues, "amenity", code)
        if catalog is None:
            continue
        fee_label = c.text(item.get("fee_label"))
        free_type = item.get("free_type")
        fee = ("free" if free_type == 0 else "paid" if free_type == 1 else None)
        details = [{"title": c.text(d.get("title")), "text": [c.text(t) for t in d.get("text") or [] if t]}
                   for d in item.get("additional_info") or [] if isinstance(d, dict)]
        row = validated(M.HotelAmenity, {
            "trip_amenity_id": code_int, "is_available": item.get("is_available") is not False,
            "is_popular": bool(item.get("is_highlight")), "fee": fee, "locale": b.locale,
            "fee_label": fee_label, "details": [d for d in details if d["text"]],
        }, issues, "amenity", code)
        if row:
            seen.add(code_int)
            b.amenities.append(catalog)
            b.hotel_amenities.append(row)
    if no_code:  # gộp thành một cảnh báo mỗi khách sạn
        issues.add("unknown_amenity_code", "amenity", value=", ".join(no_code[:10]),
                   detail=f"{len(no_code)} mục bị bỏ")
    if not b.hotel_amenities:
        issues.add("no_amenities", "hotel")


def extract_reservation_tips(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    tips = ((api_data(dump, ADDITIONAL_API).get("hotelReservationTips") or {}).get("tipList")) or []
    lines = []
    for tip in tips:
        title = c.text(tip.get("title"))
        for detail in tip.get("details") or []:
            for item in detail.get("items") or []:
                text = c.text(item.get("content"))
                if text:
                    lines.append((title, text))
    if lines:
        row = validated(M.PolicySection, {
            "locale": b.locale, "section_code": "reservationTip", "sort_order": len(b.policy_sections),
            "title": "Reservation tips" if b.locale == "en" else "Lưu ý đặt phòng", "lines": lines,
        }, issues, "policy", "reservationTip")
        if row:
            b.policy_sections.append(row)


def _smoking(info: dict) -> str:
    kind = info.get("type")
    return {1: "smoking", 2: "non_smoking", 3: "partial"}.get(kind, "unknown") if info else "unknown"


def _wifi(info: dict) -> str | None:
    if not info:
        return None
    title = str(info.get("title") or info.get("content") or "").lower()
    if "free" in title or "miễn phí" in title:
        return "free"
    if "fee" in title or "phí" in title or "charge" in title:
        return "paid"
    if "no " in title or "không" in title:
        return "none"
    return "unknown"


def extract_rooms(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    data = api_data(dump, ROOM_API)
    normalized = dump.get("normalized") or {}
    # Chỉ tin cờ của crawler: isRoomListSoldOut của Trip.com có thể True
    # ngay cả khi vẫn còn gói giá bán được (gặp thật ở hotel 134013415).
    b.rooms_sold_out = bool(normalized.get("rooms_sold_out"))
    physic = data.get("physicRoomMap") or {}
    sales = data.get("saleRoomMap") or {}
    if not physic:
        issues.add("rooms_sold_out" if b.rooms_sold_out else "no_room_api", "hotel")
        return

    # popup: Trip.com có lúc khóa theo mã phòng, có lúc theo khóa gói "id_roomCode"
    pops_by_room: dict[str, dict] = {}
    for key, pop in (api_data(dump, POP_API).get("roomPopInfo") or {}).items():
        room_id = key if key in physic else str((sales.get(key) or {}).get("physicalRoomId") or "")
        if room_id and room_id not in pops_by_room:
            pops_by_room[room_id] = pop

    seen_rooms: set[int] = set()
    for key, room in physic.items():
        room_id = room.get("id") or key
        try:
            room_int = int(room_id)
        except (TypeError, ValueError):
            issues.add("record_invalid", "room", key=room_id, field="id", value=room_id)
            continue
        if room_int in seen_rooms:
            issues.add("duplicate_room", "room", key=room_int)
            continue
        pop = pops_by_room.get(str(room_int)) or {}
        if not pop:
            issues.add("popup_missing", "room", key=room_int)
        basic = pop.get("roomBasicInfo") or {}
        policy = pop.get("policyInfo") or {}
        house = room.get("houseTypeInfo") or {}
        area_text = c.text((room.get("areaInfo") or {}).get("title") or (basic.get("areaInfo") or {}).get("content"))
        area_min, area_max = c.area(area_text, room_int)
        guest_text = c.text((basic.get("guestInfo") or {}).get("content"))
        add_bed = (basic.get("bedInfo") or {}).get("addBed") or {}
        row = validated(M.RoomType, {
            "trip_room_id": room_int, "area_sqm": area_min, "area_sqm_max": area_max,
            "max_adults": first_int(guest_text) or None,
            "bed_count": c.count(house.get("bedCount")), "bedroom_count": c.count(house.get("bedRoomCount")),
            "bathroom_count": c.count(house.get("bathRoomCount")),
            "living_room_count": c.count(house.get("livingRoomCount")),
            "window_type": (room.get("windowInfo") or {}).get("type"),
            "smoking": _smoking(room.get("smokeInfo") or {}), "wifi": _wifi(room.get("wifiInfo") or {}),
            "extra_bed": ("no" if add_bed.get("type") == "addBedFiltered" else None),
            "rent_type": house.get("rentType"), "property_type": house.get("propertyType"),
            "view_id": (room.get("outdoorLandscapeInfo") or {}).get("id"),
            "sort_order": room.get("physicRank"),
        }, issues, "room", room_int)
        if row is None:
            continue
        beds = [{"room": c.text(bed.get("roomName")), "beds": [c.text(x) for x in bed.get("detail") or [] if x]}
                for bed in (((room.get("bedInfo") or {}).get("cpxBedInfo") or {}).get("bedDetail") or [])]
        text = validated(M.RoomTypeI18n, {
            "trip_room_id": room_int, "locale": b.locale, "name": c.text(room.get("name")),
            "bed_summary": c.text((room.get("bedInfo") or {}).get("title")), "bed_details": beds,
            "area_text": area_text,
            "view_text": c.text((room.get("outdoorLandscapeInfo") or {}).get("title")),
            "guest_text": guest_text,
            "extra_bed_text": c.text((basic.get("bedInfo") or {}).get("addBedContent")),
            "child_policy": c.text((policy.get("childPolicy") or {}).get("textList")),
            "floor_text": c.text((room.get("floorInfo") or {}).get("title") or (basic.get("floorInfo") or {}).get("content")),
            "rent_text": c.text(room.get("rent")),
            "house_note": c.text(house.get("houseTypeExtraDesc")),
            "special_note": c.text((policy.get("specialNote") or {}).get("textList")),
        }, issues, "room", room_int)
        if text is None:  # không có tên phòng → bỏ cả loại phòng
            continue
        seen_rooms.add(room_int)
        b.rooms.append(row)
        b.room_i18n.append(text)
        urls: list[str] = []
        for pic in room.get("pictureInfo") or []:
            url = c.image_url(pic.get("url"))
            if url and url not in urls:
                urls.append(url)
        for order, url in enumerate(urls):
            image = validated(M.RoomImage, {"trip_room_id": room_int, "url": url, "sort_order": order},
                              issues, "room_image", url)
            if image:
                b.room_images.append(image)
        facility = room.get("faciltityInfo") or {}   # (sic) Trip.com viết sai chính tả
        top = {f.get("id") for f in facility.get("topPopularFacility") or []}
        seen_codes: set[int] = set()
        for group in facility.get("list") or []:
            for item in group.get("subList") or []:
                code = item.get("id")
                if not isinstance(code, int) or code <= 0 or code in seen_codes:
                    continue
                seen_codes.add(code)
                catalog = validated(M.Amenity, {"trip_amenity_id": code,
                                                "trip_category_id": group.get("id"), "locale": b.locale,
                                                "name": c.text(item.get("title")),
                                                "category_name": c.text(group.get("title"))},
                                    issues, "amenity", code)
                if catalog is None:
                    continue
                b.amenities.append(catalog)
                free_type = item.get("freeType")
                b.room_amenities.append(M.RoomAmenity(
                    trip_room_id=room_int, trip_amenity_id=code, is_highlight=code in top,
                    fee="free" if free_type == 0 else "paid" if free_type == 1 else None))
    extract_offers(b, dump, data, pops_by_room, c, issues)


def extract_offers(b: Bundle, dump: dict, data: dict, pops_by_room: dict, c: Cleaner, issues: Issues) -> None:
    normalized = dump.get("normalized") or {}
    try:
        check_in = date.fromisoformat(str(normalized.get("check_in")))
        check_out = date.fromisoformat(str(normalized.get("check_out")))
    except ValueError:
        issues.add("bad_date", "hotel", field="check_in", value=normalized.get("check_in"),
                   detail="không có ngày nhận/trả phòng — bỏ gói giá")
        return
    captured = (b.crawled_at or datetime.now()).date()
    found = re.search(r"[?&]adult=(\d+)", str(dump.get("url") or ""))
    adults = int(found.group(1)) if found else 2
    room_ids = {r.trip_room_id for r in b.rooms}
    seen: set[str] = set()
    low, high = PRICE_RANGE.get(b.currency, (None, None))

    for key, sale in (data.get("saleRoomMap") or {}).items():
        sale_id, code = sale.get("id"), sale.get("roomCode")
        offer_key = f"{sale_id}_{code}" if code else str(key)
        if offer_key in seen:
            issues.add("duplicate_offer", "offer", key=offer_key)
            continue
        room_id = sale.get("physicalRoomId")
        if room_id not in room_ids:
            issues.add("orphan_offer", "offer", key=offer_key, field="physicalRoomId", value=room_id)
            continue

        price_info = sale.get("priceInfo") or {}
        total_info = sale.get("totalPriceInfo") or {}
        booking = sale.get("bookingStatusInfo") or {}
        cancel = sale.get("cancelInfo") or {}
        payment = sale.get("paymentInfo") or {}
        meal = sale.get("mealInfo") or {}
        partner = sale.get("partnerInfo") or {}

        price = to_decimal(price_info.get("price"))
        total = to_decimal(sale.get("comparingAmount"))
        tax = to_decimal((total_info.get("payTax") or {}).get("price"))
        original = to_decimal(price_info.get("deletePricewithOutCurrency"))
        if booking.get("isHidePrice") and not price:
            issues.add("price_hidden", "offer", key=offer_key, value=price_info.get("displayPrice"))
            continue
        if price is None:
            issues.add("price_not_positive", "offer", key=offer_key, field="price", value=price_info.get("price"))
            continue
        if original is not None and original <= price:
            issues.fix("fix_original_price")
            original = None
        if total is not None and total < price:
            issues.add("total_below_price", "offer", key=offer_key, field="total_price",
                       value=f"{total} < {price}")
        if tax is not None and total is not None and tax > total:
            issues.add("tax_above_total", "offer", key=offer_key, field="taxes_fees", value=f"{tax} > {total}")
            continue
        if low is not None and not (low <= price <= high):
            issues.add("price_out_of_range", "offer", key=offer_key, field="price_per_night",
                       value=f"{price} {b.currency}")

        remaining = booking.get("remainRoomQuantity")
        if remaining == 9999:
            issues.fix("fix_remaining_9999")
            remaining = None
        tiers, tier_text, free_until = [], [], None
        for number, tier in enumerate(cancel.get("ladderDetailInfo") or []):
            starts = parse_iso(tier.get("hotelLocalStartTime")) or parse_ms_date(tier.get("startTime"))
            ends = parse_iso(tier.get("hotelLocalDeadline")) or parse_ms_date(tier.get("deadline"))
            ratio = tier.get("ratio")
            if ratio is None:
                continue
            tiers.append({"tier_no": number, "starts_at": starts, "ends_at": ends,
                          "penalty_ratio": Decimal(str(ratio))})
            tier_text.append({"when": c.text(tier.get("localTimeDesc")), "title": c.text(tier.get("policyTitle")),
                              "fee_text": c.text(tier.get("policyDecs"))})
            if ratio == 0 and ends:
                free_until = ends
        labels, label_texts = [], set()
        for x in (sale.get("discountLabels") or []) + (sale.get("priceLabelList") or []):
            label = c.text(x.get("text")) if isinstance(x, dict) else None
            if label and label not in label_texts:      # cùng nhãn xuất hiện ở cả hai danh sách
                label_texts.add(label)
                labels.append({"text": label, "hover": c.text(x.get("hover"))})
        pop_policy = (pops_by_room.get(str(room_id)) or {}).get("policyInfo") or {}

        offer = validated(M.RoomOffer, {
            "trip_offer_key": offer_key, "trip_sale_room_id": sale_id, "trip_room_id": room_id,
            "room_code": code, "check_in": check_in, "check_out": check_out, "adults": adults,
            "captured_date": captured, "meal_type": meal.get("mealType"),
            "breakfast_included": (meal.get("mealType") not in (None, 0)) if meal else None,
            "cancel_type": cancel.get("type"),
            "free_cancellation": total_info.get("isFreeCancel"), "free_cancel_until": free_until,
            "payment_type": {0: "prepay", 1: "pay_at_hotel"}.get(payment.get("type"), "unknown"),
            "instant_confirm": (sale.get("confirmInfo") or {}).get("type") == 1,
            "max_guests": (sale.get("guestCountInfo") or {}).get("guestCount") or None,
            "remaining_rooms": remaining, "is_sold_out": bool(booking.get("isFullRoom") and remaining == 0),
            "is_lowest_price": bool(sale.get("isStartPriceRoom")),
            "is_partner_offer": bool(partner or pop_policy.get("providerPolicy")),
            "tiers": tiers,
        }, issues, "offer", offer_key)
        if offer is None:
            continue
        price_row = validated(M.RoomOfferPrice, {
            "trip_offer_key": offer_key, "currency": b.currency, "price_per_night": price,
            "total_price": total, "taxes_fees": tax, "original_price": original,
        }, issues, "offer", offer_key)
        if price_row is None:
            continue
        text = validated(M.RoomOfferI18n, {
            "trip_offer_key": offer_key, "locale": b.locale,
            "title": c.text((sale.get("titleInfo") or {}).get("title")),
            "meal_text": c.text(meal.get("title")),
            "cancel_title": c.text(cancel.get("title")), "cancel_detail": c.text(cancel.get("hover")),
            "payment_text": c.text(payment.get("paymentTitleNew")),
            "confirm_text": c.text((sale.get("confirmInfo") or {}).get("title")),
            "cancel_tiers": tier_text, "discount_labels": labels,
            "partner_text": c.text(partner.get("title") or (pop_policy.get("providerPolicy") or {}).get("title")),
        }, issues, "offer", offer_key)
        if text is None:
            continue
        seen.add(offer_key)
        b.offers.append(offer)
        b.offer_prices.append(price_row)
        b.offer_i18n.append(text)

    # Lớp 3: loại phòng không có gói nào
    with_offers = {o.trip_room_id for o in b.offers}
    if b.rooms and not b.offers and not b.rooms_sold_out:
        issues.add("rooms_without_offers", "hotel")
    elif b.offers:
        for room in b.rooms:
            if room.trip_room_id not in with_offers:
                issues.add("room_without_offers", "room", key=room.trip_room_id)

    if b.offers:
        cheapest = min(b.offer_prices, key=lambda p: p.price_per_night)
        snapshot = validated(M.PriceSnapshot, {
            "check_in": check_in, "check_out": check_out, "currency": b.currency, "captured_date": captured,
            "min_price": cheapest.price_per_night,
            "min_total": min((p.total_price for p in b.offer_prices if p.total_price), default=None),
            "source": "detail",
        }, issues, "snapshot")
        if snapshot:
            b.snapshots.append(snapshot)


def extract_reviews(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    data = api_data(dump, COMMENT_API)
    rating = data.get("commentRating") or {}
    if not rating and not data.get("totalCount"):
        return
    scale = rating.get("fullRating") or 10

    def score(name):
        value = rating.get(name)
        return None if value in (None, "", 0) else Decimal(str(value))

    ai = data.get("aiSummaryEntities")
    if isinstance(ai, dict):
        ai = {k: v for k, v in ai.items() if "icon" not in k.lower() and v not in (None, "", [], {})}
    try:
        row = M.ReviewSummary(
            rating_overall=score("ratingAll"), rating_location=score("ratingLocation"),
            rating_facility=score("ratingFacility"), rating_service=score("ratingService"),
            rating_cleanliness=score("ratingRoom"), review_count=data.get("totalCount"),
            rating_scale=scale, locale=b.locale, level_text=c.text(rating.get("commentLevel")),
            ai_summary=ai or None)
    except ValidationError as exc:
        issues.add("rating_out_of_scale", "review", value=str(rating.get("ratingAll")),
                   detail=exc.errors()[0].get("msg"))
        return
    b.review = row
    seen: set[int] = set()
    floating = data.get("ratingFloatingLayerInfo") or {}
    for items, sentiment in ((data.get("commentTagList") or [], None),
                             (floating.get("recommendTagList") or [], "positive"),
                             (floating.get("negativeTagList") or [], "negative")):
        for tag in items:
            tag_id = tag.get("id")
            if not isinstance(tag_id, int) or tag_id in seen:
                continue
            kind = sentiment or {1: "positive", 2: "negative"}.get(tag.get("type"), "neutral")
            row = validated(M.ReviewTag, {"locale": b.locale, "trip_tag_id": tag_id,
                                          "name": c.text(tag.get("name")),
                                          "mention_count": tag.get("commentCount") if (tag.get("commentCount") or 0) >= 0 else None,
                                          "sentiment": kind}, issues, "review_tag", tag_id)
            if row:
                seen.add(tag_id)
                b.review_tags.append(row)


def extract_nearby(b: Bundle, dump: dict, c: Cleaner, issues: Issues) -> None:
    mode = {"LINEAR_DISTANCE": "straight_line", "WALK": "walk", "WALKING": "walk", "DRIVE": "drive", "DRIVING": "drive"}
    seen: set[int] = set()
    order = 0
    for group in api_data(dump, NEARBY_API).get("placeInfoList") or []:
        for place in group.get("places") or []:
            poi = place.get("id")
            if not isinstance(poi, int) or poi in seen:
                continue
            lat, lng = c.coords(place.get("lat"), place.get("lng"), "place", poi)
            distance = to_decimal(place.get("distance"))
            if distance is not None and distance > 50:
                issues.add("nearby_far", "place", key=poi, field="distance_km", value=str(distance))
            row = validated(M.NearbyPlace, {
                "trip_poi_id": poi, "poi_type": place.get("poiType"), "latitude": lat, "longitude": lng,
                "locale": b.locale, "name": c.text(place.get("name")),
                "kind": c.text((place.get("tagNames") or [None])[0]),
                "group_code": group.get("id") if isinstance(group.get("id"), int) else 0,
                "group_name": c.text(group.get("name")),
                "distance_km": distance.quantize(Decimal("0.001")) if distance is not None else None,
                "travel_mode": mode.get(str(place.get("arrivalType") or "").upper(), "unknown"),
                "distance_text": c.text(place.get("distanceDesc")), "sort_order": order,
            }, issues, "place", poi)
            if row:
                seen.add(poi)
                b.nearby.append(row)
                order += 1


# ============================================================== điểm vào
def build_bundle(dump: dict, *, raw_locale: str, currency: str, raw_path: str | None = None,
                 file_hotel_id: str | None = None, detail: dict | None = None) -> tuple[Bundle | None, Issues]:
    locale = short_locale(raw_locale)
    issues = Issues(file_hotel_id, locale, raw_path)
    hotel_id = check_response(dump, raw_locale, currency, file_hotel_id, issues)
    if hotel_id is None:
        return None, issues
    issues.trip_hotel_id = hotel_id
    crawled = parse_iso((dump.get("normalized") or {}).get("crawled_at"))
    b = Bundle(trip_hotel_id=hotel_id, locale=locale, raw_locale=raw_locale, currency=currency,
               crawled_at=crawled, raw_path=raw_path)
    c = Cleaner(issues, locale)

    detail = detail or find_detail(dump)
    master = str(((detail or {}).get("hotelBaseInfo") or {}).get("masterHotelId") or "")
    if detail and master and master != hotel_id:
        issues.add("detail_mismatch", "hotel", value=f"hotelDetailResponse của {master}")
        detail = None
    if detail:
        b.has_detail = True
        extract_detail(b, detail, c, issues)
    else:
        issues.add("no_detail_block", "hotel")
    extract_hotel_i18n(b, dump, c, issues)
    if b.hotel_i18n is None:
        return None, issues
    extract_images(b, dump, c, issues)
    extract_amenities(b, dump, c, issues)
    extract_reservation_tips(b, dump, c, issues)
    extract_rooms(b, dump, c, issues)
    extract_reviews(b, dump, c, issues)
    extract_nearby(b, dump, c, issues)
    if issues.hotel_rejected():
        return None, issues
    return b, issues


def pair_check(b: Bundle, other: Bundle | None, issues: Issues) -> None:
    """Lớp 3 giữa hai thứ tiếng: cùng ngày nhận phòng, cùng danh sách loại phòng."""
    if other is None:
        issues.add("pair_missing", "hotel")
        return
    mine = {(o.check_in, o.check_out) for o in b.offers}
    theirs = {(o.check_in, o.check_out) for o in other.offers}
    if mine and theirs and mine != theirs:
        issues.add("pair_checkin_mismatch", "hotel",
                   value=f"{b.locale}={sorted(d[0].isoformat() for d in mine)} "
                         f"{other.locale}={sorted(d[0].isoformat() for d in theirs)}")
    rooms_mine = {r.trip_room_id for r in b.rooms}
    rooms_theirs = {r.trip_room_id for r in other.rooms}
    if rooms_mine and rooms_theirs and rooms_mine != rooms_theirs:
        issues.add("pair_room_mismatch", "hotel",
                   value=f"chỉ {b.locale}: {len(rooms_mine - rooms_theirs)}, chỉ {other.locale}: {len(rooms_theirs - rooms_mine)}")
