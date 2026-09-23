"""Danh mục quy tắc kiểm tra dữ liệu v2 và bộ gom lỗi.

MỌI quy tắc nằm trong bảng RULES dưới đây, mỗi dòng gồm (lớp, mức, mô tả).
Muốn đổi một quy tắc từ "cảnh báo" sang "lỗi" (hoặc ngược lại) thì chỉ sửa
đúng một chỗ này, không phải sửa code kiểm tra.

Mức:
  error    → không lưu bản ghi đó (entity='hotel' thì bỏ cả khách sạn)
  warning  → vẫn lưu, ghi vào v2.load_rejects để xem lại
  fixed    → đã tự chuẩn hóa (9999 → NULL, ft² → m²…), chỉ đếm số lần

Lớp:
  1 = response (có phải dữ liệu thật không)
  2 = từng bản ghi (kiểu, khoảng giá trị)
  3 = liên kết trong một khách sạn và giữa hai thứ tiếng
  4 = cả đợt nạp
"""
from __future__ import annotations

import collections
from dataclasses import asdict, dataclass

ERROR, WARNING, FIXED = "error", "warning", "fixed"

RULES: dict[str, tuple[int, str, str]] = {
    # ---------------------------------------------------------------- lớp 1
    "raw_unreadable":        (1, ERROR,   "Không đọc được file raw"),
    "blocked":               (1, ERROR,   "Trip.com chặn (mã 4030 / trang đăng nhập / Antibot) — không phải dữ liệu thật"),
    "page_dead":             (1, ERROR,   "Trang khách sạn không còn tồn tại"),
    "crawl_failed":          (1, ERROR,   "Lượt cào thất bại; không nạp dữ liệu thiếu vào v2"),
    "wrong_locale":          (1, ERROR,   "Raw không đúng ngôn ngữ yêu cầu"),
    "wrong_currency":        (1, ERROR,   "Giá trong raw không đúng tiền tệ yêu cầu"),
    "missing_hotel_id":      (1, ERROR,   "Không xác định được mã khách sạn"),
    "hotel_id_mismatch":     (1, ERROR,   "Mã khách sạn trong raw khác tên file"),
    "no_detail_block":       (1, WARNING, "Raw chưa có khối hotelDetailResponse — bỏ qua sao, tọa độ, chính sách có cấu trúc"),
    "detail_mismatch":       (1, WARNING, "Khối hotelDetailResponse thuộc khách sạn khác — bỏ qua khối này"),
    "no_room_api":           (1, WARNING, "Không có response danh sách phòng (không phải hết phòng)"),
    "rooms_sold_out":        (1, WARNING, "Trip.com báo hết phòng ngày này — không có gói giá"),
    # ---------------------------------------------------------------- lớp 2
    "record_invalid":        (2, ERROR,   "Bản ghi sai kiểu hoặc ngoài khoảng cho phép"),
    "missing_name":          (2, ERROR,   "Thiếu tên"),
    "price_not_positive":    (2, ERROR,   "Giá ≤ 0"),
    "price_hidden":          (2, WARNING, "Giá chỉ hiện cho thành viên đăng nhập (\"$?\") — bỏ gói này"),
    "price_out_of_range":    (2, WARNING, "Giá bất thường so với tiền tệ (quá rẻ/quá đắt)"),
    "total_below_price":     (2, WARNING, "Tổng tiền (gồm thuế) nhỏ hơn giá một đêm"),
    "tax_above_total":       (2, ERROR,   "Tiền thuế lớn hơn tổng tiền"),
    "area_implausible":      (2, WARNING, "Diện tích phòng bất thường — để trống"),
    "area_unparsed":         (2, WARNING, "Không đọc được diện tích"),
    "coords_invalid":        (2, WARNING, "Tọa độ không hợp lệ — để trống"),
    "rating_out_of_scale":   (2, ERROR,   "Điểm đánh giá ngoài thang điểm"),
    "renovated_before_open": (2, WARNING, "Năm sửa chữa trước năm mở cửa"),
    "promo_description":     (2, WARNING, "Mô tả là câu quảng cáo chung, không phải mô tả khách sạn"),
    "no_description":        (2, WARNING, "Không có mô tả"),
    "no_images":             (2, WARNING, "Không có ảnh"),
    "no_amenities":          (2, WARNING, "Không có tiện nghi"),
    "nearby_far":            (2, WARNING, "Địa điểm 'lân cận' xa hơn 50 km"),
    "bad_url":               (2, ERROR,   "URL ảnh không hợp lệ"),
    "bad_date":              (2, ERROR,   "Ngày không hợp lệ (trả phòng ≤ nhận phòng…)"),
    # ---------------------------------------------------------------- lớp 3
    "orphan_offer":          (3, ERROR,   "Gói giá trỏ tới loại phòng không có"),
    "duplicate_offer":       (3, ERROR,   "Trùng khóa gói giá (id + roomCode)"),
    "duplicate_room":        (3, ERROR,   "Trùng mã loại phòng"),
    "rooms_without_offers":  (3, WARNING, "Có loại phòng nhưng không có gói giá nào"),
    "room_without_offers":   (3, WARNING, "Loại phòng không có gói giá (có thể hết phòng loại này)"),
    "popup_missing":         (3, WARNING, "Loại phòng không có thông tin chi tiết (popup)"),
    "unknown_amenity_code":  (3, WARNING, "Tiện nghi không có mã (vd ngôn ngữ phục vụ) — không lưu được vào danh mục"),
    "pair_checkin_mismatch": (3, WARNING, "Bản Anh và bản Việt khác ngày nhận phòng — gói giá không ghép được"),
    "pair_room_mismatch":    (3, WARNING, "Bản Anh và bản Việt có danh sách loại phòng khác nhau"),
    "pair_missing":          (3, WARNING, "Chưa có raw thứ tiếng còn lại cho khách sạn này"),
    "db_error":              (3, ERROR,   "PostgreSQL từ chối khi ghi — đã rollback cả khách sạn"),
    # ---------------------------------------------------------------- lớp 4
    "gate_blocked_ratio":    (4, ERROR,   "Tỷ lệ raw bị chặn quá cao"),
    "gate_reject_ratio":     (4, ERROR,   "Tỷ lệ khách sạn bị loại quá cao"),
    "gate_rooms_drop":       (4, ERROR,   "Tỷ lệ khách sạn có phòng giảm mạnh so với lần nạp trước"),
    "gate_images_drop":      (4, ERROR,   "Tỷ lệ khách sạn có ảnh giảm mạnh so với lần nạp trước"),
    # ---------------------------------------------------------------- tự sửa
    "fix_remaining_9999":    (2, FIXED,   "Số phòng còn 9999 ('còn nhiều') → NULL"),
    "fix_negative_count":    (2, FIXED,   "Số phòng ngủ/tắm -1 → NULL"),
    "fix_area_unit":         (2, FIXED,   "Diện tích ft² → m²"),
    "fix_area_range":        (2, FIXED,   "Diện tích dạng khoảng → tách min/max"),
    "fix_markup":            (2, FIXED,   "Bỏ thẻ định dạng {0}…{/0} trong chữ"),
    "fix_whitespace":        (2, FIXED,   "Chuẩn hóa khoảng trắng / chuỗi rỗng → NULL"),
    "fix_original_price":    (2, FIXED,   "Giá gạch không lớn hơn giá bán → bỏ giá gạch"),
    "fix_image_original":    (2, FIXED,   "URL ảnh thu nhỏ/watermark → ảnh gốc"),
    "fix_image_duplicate":   (2, FIXED,   "Bỏ ảnh trùng"),
    "fix_zero_coords":       (2, FIXED,   "Tọa độ 0,0 → NULL"),
    "fix_local_name_prefix": (2, FIXED,   "Bỏ tiền tố 'Local hotel name:'"),
}

# Ngưỡng chốt chặn cả đợt (lớp 4)
GATE = {
    "max_blocked_ratio": 0.20,     # > 20% raw bị chặn → dừng
    "max_reject_ratio": 0.10,      # > 10% khách sạn bị loại → dừng
    "max_rooms_drop_pts": 30.0,    # tỷ lệ có phòng giảm > 30 điểm % so với lần trước → dừng
    "max_images_drop_pts": 30.0,
}


@dataclass
class Issue:
    rule: str
    layer: int
    severity: str
    entity: str
    message: str
    trip_hotel_id: str | None = None
    locale: str | None = None
    entity_key: str | None = None
    field: str | None = None
    value: str | None = None
    raw_path: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Issues:
    """Gom lỗi/cảnh báo/tự sửa của một khách sạn (hoặc cả đợt)."""

    def __init__(self, trip_hotel_id: str | None = None, locale: str | None = None,
                 raw_path: str | None = None):
        self.trip_hotel_id = trip_hotel_id
        self.locale = locale
        self.raw_path = raw_path
        self.items: list[Issue] = []
        self.fixed: collections.Counter = collections.Counter()

    def add(self, rule: str, entity: str, *, key=None, field=None, value=None,
            detail: str | None = None) -> str:
        layer, severity, text = RULES[rule]
        if severity == FIXED:
            self.fixed[rule] += 1
            return severity
        message = f"{text}: {detail}" if detail else text
        shown = None if value is None else str(value)[:300]
        self.items.append(Issue(rule, layer, severity, entity, message, self.trip_hotel_id,
                                self.locale, None if key is None else str(key), field, shown,
                                self.raw_path))
        return severity

    def fix(self, rule: str, count: int = 1) -> None:
        self.fixed[rule] += count

    # tiện dụng
    def errors(self) -> list[Issue]:
        return [i for i in self.items if i.severity == ERROR]

    def warnings(self) -> list[Issue]:
        return [i for i in self.items if i.severity == WARNING]

    def hotel_rejected(self) -> bool:
        """Lỗi ở cấp khách sạn → bỏ cả khách sạn."""
        return any(i.severity == ERROR and i.entity == "hotel" for i in self.items)
