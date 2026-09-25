# Ghi chú thống nhất với anh mentor — bảng `trip_tmp_property_translation`

## Cấu trúc bảng

```
row_uuid | property_id | type | section_type ‖ lang | field | value
└──── giống nhau ở mọi ngôn ngữ ─────────────┘ ‖ └── riêng từng ngôn ngữ ──┘
```

- `property_id` phải là **BIGINT** — giá trị lớn nhất đang thấy: 131.793.034 (vượt ngưỡng INTEGER).
- Khoá nghiệp vụ: `UNIQUE (row_uuid, lang, field)`. Đã kiểm tra trên 15.706 dòng, không vi phạm.

## `row_uuid` — sinh bằng md5, không chứa ngôn ngữ

```
DESCRIPTION  →  md5( trip_hotel_id : 'DESCRIPTION' )
POLICY       →  md5( trip_hotel_id : 'POLICY'      : section_code )   vd 'checkInAndOut'
SURROUNDING  →  md5( trip_hotel_id : 'SURROUNDING' : trip_poi_id  )   vd 900003
```

Nguyên liệu đều là mã bất biến theo ngôn ngữ, nên bản VI và EN của cùng một bản
ghi ra **cùng một uuid** → ghép cặp được. Chạy lại export bao nhiêu lần cũng ra
đúng uuid cũ, import đè lên được, không sinh bản ghi trùng.

## `section_type`

| type | giá trị | nguồn |
|---|---|---|
| DESCRIPTION | `hotelInfo` | cố định |
| POLICY | `checkInAndOut`, `pet`, `breakfast`… | key gốc trong `hotelPolicyInfo` của Trip.com (giữ cả lỗi chính tả `quiteTime` của họ) |
| SURROUNDING | `02_Giao thông` / `02_Transport` | `<mã nhóm 2 chữ số>_<tên nhóm theo ngôn ngữ đó>` |

Mã nhóm là `placeInfoList[].id` của Trip.com, đệm 0 cho đủ 2 chữ số để sắp xếp
theo chuỗi vẫn đúng thứ tự. Tách lại:

```sql
split_part(section_type, '_', 1)::int              -- mã nhóm
substr(section_type, strpos(section_type,'_') + 1) -- tên nhóm
```

### Điểm đã biết và chấp nhận

Trip.com **xếp nhóm khác nhau theo ngôn ngữ**. Cùng khách sạn 118050925:

```
EN:  2 Transport   3 Landmarks   4 Dining   5 Shopping
VI:  2 Giao thông  3 Điểm nổi bật           5 Mua Sắm
```

Nhóm `4 Dining` chỉ bản tiếng Anh có. Hệ quả với `section_type` dạng trên:

- **Cùng một `row_uuid` mang hai `section_type` khác nhau** ở hai ngôn ngữ
  (`03_Điểm nổi bật` ↔ `03_Landmarks`). Không phải lỗi dữ liệu.
- Đếm nhóm **phải lọc kèm `lang`**, không có con số chung cho cả hai.
- Lọc một nhóm chỉ ra một ngôn ngữ; muốn bản dịch kia phải tra ngược `row_uuid`.

## Tool import

- **Commit mỗi 200 dòng** (anh mentor dặn). Không gom cả file vào một
  transaction: lỗi ở cuối là rollback sạch, phải chạy lại từ đầu. Cũng đừng
  commit từng dòng vì mỗi commit là một lần ghi đĩa.
- Nạp lại khi `section_type` đổi giá trị: `TRUNCATE` trước, hoặc upsert theo
  `(row_uuid, lang, field)`.

## Số liệu file dump 20 khách sạn Copenhagen

```
15.706 dòng · 1.869 bản ghi · 20 khách sạn

type          bản ghi   đủ VI+EN   chỉ 1 thứ tiếng
DESCRIPTION        20         20                 0
POLICY            186        184                 2   (đều là reservationTip)
SURROUNDING     1.663      1.289               374   (361 là nhóm 04_Dining, chỉ EN)
```

`reservationTip` là mục **crawler tự định nghĩa**, Trip.com không có key này —
nên bản EN không tồn tại, không phải crawl sót.
