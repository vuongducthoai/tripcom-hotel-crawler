"""Model Pydantic cho từng bảng v2 — lớp kiểm tra 2 (từng bản ghi).

Mỗi model ứng với một bảng trong migrations_v2 (001 + 002). Khoảng giá trị ở
đây khớp với ràng buộc CHECK trong SQL, để lỗi bị chặn ngay trong Python
(có lý do rõ ràng, ghi vào load_rejects) thay vì PostgreSQL ném lỗi giữa chừng.

extra="forbid": viết nhầm tên trường khi bóc tách sẽ báo lỗi ngay khi test.
Các khóa tự nhiên (trip_room_id, trip_offer_key…) dùng để nối bảng cha–con
lúc ghi DB, thay cho id tự tăng chưa có.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Allow = Literal["yes", "no", "on_request", "unknown"]
Locale = Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
Currency = Field(pattern=r"^[A-Z]{3}$")
Url = Field(pattern=r"^https?://\S+$")
Count = Field(default=None, ge=0, le=50)


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ địa lý
class Country(Row):
    trip_country_id: int = Field(gt=0)
    iso2: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$")
    name: Optional[str] = None


class City(Row):
    trip_city_id: int = Field(gt=0)
    trip_country_id: int = Field(gt=0)
    trip_province_id: Optional[int] = None
    utc_offset_sec: Optional[int] = Field(default=None, ge=-14 * 3600, le=14 * 3600)
    name: Optional[str] = None
    province_name: Optional[str] = None


# ------------------------------------------------------------------ khách sạn
class Hotel(Row):
    trip_hotel_id: int = Field(gt=0)
    trip_city_id: Optional[int] = None
    star_level: Optional[int] = Field(default=None, ge=0, le=5)
    star_type: Optional[Literal["star", "diamond", "circle"]] = None
    is_super_star: Optional[bool] = None
    medal_type: Optional[int] = None
    open_year: Optional[int] = Field(default=None, ge=1800, le=2100)
    renovated_year: Optional[int] = Field(default=None, ge=1800, le=2100)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    is_private_host: Optional[bool] = None
    detail_url: Optional[str] = None


class HotelI18n(Row):
    locale: str = Locale
    name: str = Field(min_length=1)
    local_name: Optional[str] = None
    address: Optional[str] = None
    zone_name: Optional[str] = None
    traffic_desc: Optional[str] = None
    hotel_type: Optional[str] = None
    description: Optional[str] = None
    description_source: Optional[Literal["intro", "meta", "json_ld"]] = None
    last_booked_text: Optional[str] = None

    @model_validator(mode="after")
    def _source_needs_description(self):
        if self.description_source and not self.description:
            raise ValueError("description_source có nhưng description rỗng")
        return self


class HotelHighlight(Row):
    locale: str = Locale
    sort_order: int = Field(ge=0)
    trip_tag_id: Optional[str] = None
    title: str = Field(min_length=1)
    description: Optional[str] = None
    icon_url: Optional[str] = None


# ------------------------------------------------------------------ ảnh
class ImageCategory(Row):
    trip_category_id: int
    locale: str = Locale
    name: str = Field(min_length=1)


class HotelImage(Row):
    url: str = Url
    trip_picture_id: Optional[int] = None
    uploader: Literal["hotel", "guest"]
    trip_category_id: Optional[int] = None
    sort_order: Optional[int] = Field(default=None, ge=0)
    is_cover: bool = False


# ------------------------------------------------------------------ tiện nghi
class Amenity(Row):
    trip_amenity_id: int = Field(gt=0)
    trip_category_id: Optional[int] = None
    locale: str = Locale
    name: str = Field(min_length=1)
    category_name: Optional[str] = None
    icon: Optional[str] = None


class HotelAmenity(Row):
    trip_amenity_id: int = Field(gt=0)
    is_available: bool = True
    is_popular: bool = False
    fee: Optional[Literal["free", "paid", "unknown"]] = None
    locale: str = Locale
    fee_label: Optional[str] = None
    details: list = Field(default_factory=list)


# ------------------------------------------------------------------ chính sách
class PolicySection(Row):
    locale: str = Locale
    section_code: str = Field(min_length=1)
    sort_order: int = Field(ge=0)
    title: str = Field(min_length=1)
    lines: list[tuple[Optional[str], str]] = Field(default_factory=list)   # (nhãn, chữ)

    @field_validator("lines")
    @classmethod
    def _lines_have_text(cls, value):
        if any(not (text or "").strip() for _, text in value):
            raise ValueError("có dòng chính sách rỗng")
        return value


class HotelPolicy(Row):
    checkin_from: Optional[time] = None
    checkin_until: Optional[time] = None
    checkout_until: Optional[time] = None
    front_desk_24h: Optional[bool] = None
    min_checkin_age: Optional[int] = Field(default=None, ge=0, le=99)
    children_allowed: Optional[Allow] = None
    child_min_age: Optional[int] = Field(default=None, ge=0, le=17)
    child_free_max_age: Optional[int] = Field(default=None, ge=0, le=17)
    extra_bed: Optional[Allow] = None
    crib: Optional[Allow] = None
    breakfast_available: Optional[bool] = None
    deposit_required: Optional[bool] = None
    pets: Optional[Allow] = None
    service_animals: Optional[Allow] = None
    quiet_hours_from: Optional[time] = None
    quiet_hours_until: Optional[time] = None
    payment_methods: Optional[list[str]] = None
    parsed_from_locale: str = Locale


# ------------------------------------------------------------------ loại phòng
class RoomType(Row):
    trip_room_id: int = Field(gt=0)
    area_sqm: Optional[Decimal] = Field(default=None, gt=0, lt=10000)
    area_sqm_max: Optional[Decimal] = Field(default=None, gt=0, lt=10000)
    max_adults: Optional[int] = Field(default=None, ge=1, le=50)
    bed_count: Optional[int] = Count
    bedroom_count: Optional[int] = Count
    bathroom_count: Optional[int] = Count
    living_room_count: Optional[int] = Count
    window_type: Optional[int] = None
    smoking: Optional[Literal["non_smoking", "smoking", "partial", "unknown"]] = None
    wifi: Optional[Literal["free", "paid", "none", "unknown"]] = None
    extra_bed: Optional[Allow] = None
    rent_type: Optional[int] = None
    property_type: Optional[int] = None
    view_id: Optional[int] = None
    sort_order: Optional[int] = None

    @model_validator(mode="after")
    def _area_range(self):
        if self.area_sqm_max is not None and (self.area_sqm is None or self.area_sqm_max < self.area_sqm):
            raise ValueError("area_sqm_max nhỏ hơn area_sqm")
        return self


class RoomTypeI18n(Row):
    trip_room_id: int = Field(gt=0)
    locale: str = Locale
    name: str = Field(min_length=1)
    bed_summary: Optional[str] = None
    bed_details: list = Field(default_factory=list)
    area_text: Optional[str] = None
    view_text: Optional[str] = None
    guest_text: Optional[str] = None
    extra_bed_text: Optional[str] = None
    child_policy: Optional[str] = None
    floor_text: Optional[str] = None
    rent_text: Optional[str] = None
    house_note: Optional[str] = None
    special_note: Optional[str] = None


class RoomImage(Row):
    trip_room_id: int = Field(gt=0)
    url: str = Url
    sort_order: Optional[int] = Field(default=None, ge=0)


class RoomAmenity(Row):
    trip_room_id: int = Field(gt=0)
    trip_amenity_id: int = Field(gt=0)
    is_highlight: bool = False
    fee: Optional[Literal["free", "paid", "unknown"]] = None


# ------------------------------------------------------------------ gói giá
class CancelTier(Row):
    tier_no: int = Field(ge=0)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    penalty_ratio: Decimal = Field(ge=0, le=1)


class RoomOffer(Row):
    trip_offer_key: str = Field(min_length=3)
    trip_sale_room_id: int = Field(gt=0)
    trip_room_id: int = Field(gt=0)
    room_code: Optional[str] = None
    check_in: date
    check_out: date
    adults: int = Field(default=2, ge=1, le=50)
    captured_date: date
    meal_type: Optional[int] = None
    breakfast_included: Optional[bool] = None
    cancel_type: Optional[int] = None
    free_cancellation: Optional[bool] = None
    free_cancel_until: Optional[datetime] = None
    payment_type: Optional[Literal["prepay", "pay_at_hotel", "unknown"]] = None
    instant_confirm: Optional[bool] = None
    max_guests: Optional[int] = Field(default=None, ge=1, le=50)
    remaining_rooms: Optional[int] = Field(default=None, ge=0, lt=9999)
    is_sold_out: bool = False
    is_lowest_price: bool = False
    is_partner_offer: bool = False
    tiers: list[CancelTier] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates(self):
        if self.check_out <= self.check_in:
            raise ValueError("check_out phải sau check_in")
        return self


class RoomOfferPrice(Row):
    trip_offer_key: str
    currency: str = Currency
    price_per_night: Decimal = Field(gt=0)
    total_price: Optional[Decimal] = Field(default=None, gt=0)
    taxes_fees: Optional[Decimal] = Field(default=None, ge=0)
    original_price: Optional[Decimal] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _consistent(self):
        if self.taxes_fees is not None and self.total_price is not None and self.taxes_fees > self.total_price:
            raise ValueError("tiền thuế lớn hơn tổng tiền")
        if self.original_price is not None and self.original_price <= self.price_per_night:
            raise ValueError("giá gạch phải lớn hơn giá bán")
        return self


class RoomOfferI18n(Row):
    trip_offer_key: str
    locale: str = Locale
    title: Optional[str] = None
    meal_text: Optional[str] = None
    cancel_title: Optional[str] = None
    cancel_detail: Optional[str] = None
    payment_text: Optional[str] = None
    confirm_text: Optional[str] = None
    cancel_tiers: list = Field(default_factory=list)
    discount_labels: list = Field(default_factory=list)
    partner_text: Optional[str] = None


class PriceSnapshot(Row):
    check_in: date
    check_out: date
    currency: str = Currency
    captured_date: date
    min_price: Optional[Decimal] = Field(default=None, gt=0)
    min_total: Optional[Decimal] = Field(default=None, gt=0)
    source: Literal["list", "detail", "json_ld"]


# ------------------------------------------------------------------ đánh giá
class ReviewSummary(Row):
    rating_overall: Optional[Decimal] = Field(default=None, ge=0)
    rating_location: Optional[Decimal] = Field(default=None, ge=0)
    rating_facility: Optional[Decimal] = Field(default=None, ge=0)
    rating_service: Optional[Decimal] = Field(default=None, ge=0)
    rating_cleanliness: Optional[Decimal] = Field(default=None, ge=0)
    review_count: Optional[int] = Field(default=None, ge=0)
    rating_scale: int = 10
    locale: str = Locale
    level_text: Optional[str] = None
    ai_summary: Optional[list | dict] = None

    @model_validator(mode="after")
    def _within_scale(self):
        if self.rating_scale not in (5, 10):
            raise ValueError("thang điểm phải là 5 hoặc 10")
        for name in ("rating_overall", "rating_location", "rating_facility",
                     "rating_service", "rating_cleanliness"):
            value = getattr(self, name)
            if value is not None and value > self.rating_scale:
                raise ValueError(f"{name}={value} vượt thang {self.rating_scale}")
        return self


class ReviewTag(Row):
    locale: str = Locale
    trip_tag_id: int
    name: str = Field(min_length=1)
    mention_count: Optional[int] = Field(default=None, ge=0)
    sentiment: Optional[Literal["positive", "negative", "neutral"]] = None


# ------------------------------------------------------------------ lân cận
class NearbyPlace(Row):
    trip_poi_id: int = Field(gt=0)
    poi_type: Optional[int] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    locale: str = Locale
    name: str = Field(min_length=1)
    kind: Optional[str] = None
    group_code: int
    group_name: Optional[str] = None
    distance_km: Optional[Decimal] = Field(default=None, ge=0)
    travel_mode: Literal["walk", "drive", "straight_line", "unknown"] = "unknown"
    distance_text: Optional[str] = None
    sort_order: Optional[int] = None
