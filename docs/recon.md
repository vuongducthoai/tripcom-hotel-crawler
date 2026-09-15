# Recon Trip.com — kết luận

> Điền file này SAU khi chạy `python src/recon.py`.
> Đây là căn cứ để chọn đường A hay đường B, và là thứ anh Thắng sẽ đọc.

## 1. Trang đã khảo sát

| Tên | URL | Ngày |
|-----|-----|------|
| Đặt phòng khách sạn | https://vn.trip.com/hotels/ | |
| Khách sạn giá rẻ | | |
| Danh sách theo thành phố | | |

## 2. robots.txt

- URL: https://vn.trip.com/robots.txt
- Đường dẫn khách sạn có bị `Disallow` không:
- Có `Crawl-delay` không:
- **Đã hỏi anh Thắng chưa:** chưa / rồi — kết luận:

## 3. Có API JSON không?

- [ ] Có → điền mục 4, dùng đường A (`src/http_client.py`)
- [ ] Không → dữ liệu nằm trong HTML/SSR, dùng đường B (`src/crawl_list.py`)

## 4. Endpoint (nếu có)

| Mục | Giá trị |
|-----|---------|
| URL | |
| Method | |
| Header bắt buộc | |
| Params phân trang | |
| Có token/signature động không | |
| Số bản ghi tối đa mỗi trang | |
| Giới hạn số trang | |

### Field lấy được

| Field DB | Đường dẫn trong JSON | Ghi chú |
|----------|---------------------|---------|
| trip_hotel_id | | |
| name | | |
| address | | |
| latitude / longitude | | |
| star_rating | | |
| review_score / review_count | | |
| price_from | | |

### Field KHÔNG có ở API list (phải vào trang chi tiết)

-

## 5. Rate limit thực đo

| Tốc độ | Kết quả |
|--------|---------|
| 10 req/phút | |
| 30 req/phút | |
| 60 req/phút | |

- Ngưỡng an toàn chọn dùng:
- Có cần proxy không:

## 6. Ước lượng khối lượng

- Số khách sạn trong phạm vi đã chốt:
- Số request cần thiết:
- Thời gian chạy ước tính ở tốc độ an toàn:

## 7. Kết luận & đề xuất

-
