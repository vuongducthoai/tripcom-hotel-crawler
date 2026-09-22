# Schema v2 — dữ liệu khách sạn Trip.com (song ngữ, đa quốc gia)

File SQL: [`migrations_v2/001_schema_v2.sql`](../migrations_v2/001_schema_v2.sql)

Schema PostgreSQL riêng tên `v2`, không đụng bảng cũ. Dữ liệu TP.HCM cũ giữ nguyên để tham khảo.

## Vì sao làm lại

| Vấn đề ở bản cũ | Ví dụ | Bản v2 |
|---|---|---|
| Chính sách copy chữ từ popup rồi đoán chỗ cắt | "Sau 14:00Trả phòng"; "Đặt cọc" bị dồn vào "Bữa sáng" | Lấy từ khối có cấu trúc `hotelPolicyInfo` của trang; mỗi mục một dòng, và có thêm bảng giá trị đã chuẩn hóa để lọc |
| Không phân biệt loại phòng và gói giá | Một phòng có 3 gói (chỉ phòng / có bữa sáng / hủy miễn phí) bị gộp | `room_types` (thẻ phòng) và `room_offers` (từng gói giá) tách riêng |
| Ngôn ngữ khóa cứng `vi`/`en`, quốc gia gán cứng Việt Nam | — | Quốc gia → thành phố theo mã Trip.com; ngôn ngữ không khóa cứng |
| Tiện nghi nhận diện theo tên | Cùng tiện nghi nhưng tên khác nhau ở hai thứ tiếng thành hai bản ghi | Danh mục chung theo mã tiện nghi của Trip.com |
| Địa điểm lân cận bị nhân bản | Một ga metro gần 200 khách sạn thành 200 bản ghi | `places` dùng chung, khách sạn chỉ tham chiếu |
| Không ghi nguồn mô tả | Câu quảng cáo "Book your stay…" bị tính là mô tả | Cột `description_source` |

## Nguyên tắc

- **Bảng lõi** chứa thứ không phụ thuộc ngôn ngữ: số, mã, cờ, tọa độ, giá.
- **Bảng `*_i18n`** chứa chữ hiển thị, khóa theo `(…, locale)`. Hiện có `vi` và `en`; thêm ngôn ngữ khác không phải sửa schema.
- Mọi mã của Trip.com giữ ở cột `trip_*_id` để đối chiếu với trang web.
- Mỗi khối bảng ứng với một phần trên trang chi tiết Trip.com, để giao diện web dựng lại được như trang gốc.

## Các khối bảng

### 1. Địa lý: `countries`, `cities` (+ `_i18n`)
Mã quốc gia, thành phố, tỉnh của Trip.com; múi giờ; tên theo ngôn ngữ.

### 2. Khách sạn: `hotels`, `hotel_i18n`, `hotel_highlights`
Hạng sao (sao hay kim cương), huy hiệu, năm mở cửa, năm sửa chữa, tọa độ, chủ nhà tư nhân hay không.
Theo ngôn ngữ: tên, tên bản địa, địa chỉ, khu vực, chỉ dẫn giao thông, loại hình, mô tả kèm nguồn mô tả, và các mục "Điểm nổi bật".

### 3. Ảnh: `hotel_images`, `image_categories` (+ `_i18n`)
Ảnh gốc không watermark, do khách sạn hay khách đăng, thuộc tab nào (Nổi bật, Bên ngoài, Phòng…), thứ tự, có phải ảnh đầu trang không.

### 4. Tiện nghi: `amenities`, `amenity_categories` (+ `_i18n`), `hotel_amenities`, `hotel_amenity_details`
Danh mục chung theo mã. Mỗi khách sạn: có hay không, có phổ biến không, miễn phí hay tính phí; chi tiết kèm theo (loại, vị trí, có cần đặt trước).

### 5. Chính sách: `hotel_policies`, `hotel_policy_sections`, `hotel_policy_lines`
- **`hotel_policies`**: giá trị chuẩn hóa để lọc — giờ nhận/trả phòng, lễ tân 24/7, tuổi tối thiểu, trẻ em, giường phụ, nôi, bữa sáng, đặt cọc, thú cưng, động vật hỗ trợ, giờ yên tĩnh, phương thức thanh toán.
- **`sections` + `lines`**: từng mục, từng dòng đúng như Trip.com hiển thị, song ngữ.

### 6. Loại phòng: `room_types`, `room_type_i18n`, `room_images`, `room_amenities`
Diện tích (quy về m², giữ chữ gốc), số người lớn tối đa, số giường / phòng ngủ / phòng tắm / phòng khách, cửa sổ, hút thuốc, Wi-Fi, giường phụ (có không, giá bao nhiêu).
Theo ngôn ngữ: tên phòng, mô tả giường, chi tiết giường theo từng phòng ngủ, hướng nhìn, chính sách trẻ em riêng của phòng.

### 7. Gói giá: `room_offers`, `room_offer_prices`, `room_offer_i18n`, `hotel_price_snapshots`
- **`room_offers`**: điều kiện gói — bữa ăn, loại hủy, hủy miễn phí, trả trước hay trả tại khách sạn, xác nhận ngay, số khách, số phòng còn lại, có phải giá thấp nhất. Gắn với ngày ở và ngày lấy dữ liệu, nên giữ được lịch sử giá.
- **`room_offer_prices`**: giá theo từng tiền tệ — giá mỗi đêm, tổng gồm thuế và phí, tiền thuế, giá gạch.
- **`room_offer_i18n`**: chữ hiển thị ("Chỉ tiền phòng", "Không hoàn tiền", "Thanh toán trực tuyến").
- **`hotel_price_snapshots`**: giá thấp nhất cấp khách sạn, luôn có kể cả khi không lấy được danh sách phòng.

### 8. Đánh giá (tóm tắt): `hotel_review_summary` (+ `_i18n`), `hotel_review_tags`
Điểm tổng và điểm theo vị trí / tiện nghi / dịch vụ / sạch sẽ, số đánh giá, mức xếp hạng ("Tuyệt vời"), tóm tắt AI của Trip.com, các nhãn tích cực/tiêu cực kèm số lần nhắc tới. Không lưu nội dung từng đánh giá.

### 9. Vị trí lân cận: `places` (+ `_i18n`), `hotel_nearby_places` (+ `_i18n`)
Địa điểm dùng chung; mỗi khách sạn có khoảng cách, cách đi (đi bộ / lái xe), nhóm (giao thông, tham quan…), câu mô tả khoảng cách.

### 10. Theo dõi cào: `hotel_crawls`
Mỗi lần cào một khách sạn ở một ngôn ngữ: thời điểm, phiên bản bộ bóc tách, đường dẫn file raw, mục nào lấy được và lý do nếu bị chặn.

## Đã kiểm chứng bằng dữ liệu thật

Chạy migration trên PostgreSQL 16 (35 bảng, không lỗi), rồi đổ raw thật của 2 khách sạn (bản Anh và bản Việt) cùng một trang chi tiết đã lưu:

| Kiểm tra | Kết quả |
|---|---|
| Loại phòng khớp giữa hai thứ tiếng | 14/14 và 7/7 — dùng chung một bảng là đúng |
| Gói giá song ngữ | Mỗi gói có cả USD lẫn VND, chữ Anh lẫn Việt: "Room only / Chỉ tiền phòng", $64 / 1.666.667 ₫ |
| Chính sách có cấu trúc | 17 dòng, tách đúng từng mục; chuẩn hóa được: nhận 14:00, trả 12:00, lễ tân 24/7, 18 tuổi, trẻ 0–5 ở miễn phí, không cho thú cưng |

## Phát hiện ảnh hưởng tới crawler

1. **Cùng mã gói có thể là hai gói khác nhau.** Ví dụ một mã có một bản cho 1 khách + 1 bữa sáng và một bản cho 2 khách + 2 bữa sáng, giá khác nhau. Khóa đúng là mã + `roomCode`.
2. **Bản Việt và bản Anh phải cào cùng một ngày nhận phòng.** Nếu khác ngày thì gói giá không ghép được với nhau.
3. **Đọc giá từ trường số, không đọc chuỗi hiển thị.** Chuỗi VND có dạng `3.749.460 ₫`.
4. **`9999` phòng trống** nghĩa là "còn nhiều", phải lưu thành rỗng.
5. **Chính sách và thông tin cơ bản nằm trong khối `hotelDetailResponse`** của trang. Crawler hiện chưa lưu khối này vào raw, cần bổ sung.

## Việc tiếp theo, sau khi duyệt schema

1. Sửa crawler: lưu cả khối `hotelDetailResponse`, cào Việt và Anh cùng ngày.
2. Viết loader mới cho v2.
3. Làm lại giao diện web theo bố cục trang Trip.com, dựa trên các khối bảng trên.
4. Chạy thử một thành phố nước ngoài.
