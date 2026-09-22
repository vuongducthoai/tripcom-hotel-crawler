# Trip.com Hotel Crawler

Crawl dữ liệu khách sạn Trip.com (tiếng Việt + tiếng Anh) về PostgreSQL.

- **Người thực hiện:** Vương Đức Thoại, Trần Đăng Nguyên (S.AI20K)
- **Nghiệm thu:** Nguyễn Thạch Vũ (VSF-KD&VH DLKS-PMKD)

## Hiện trạng

- **Dữ liệu cũ (schema `public`):** 3.431 khách sạn TP.HCM, giữ nguyên để tham khảo.
- **Schema mới `v2`** ([docs/schema_v2.md](docs/schema_v2.md)): song ngữ, đa quốc gia, tách
  loại phòng / gói giá, chính sách có cấu trúc. Migration `migrations_v2/001` → `003`.
- **Kiểm tra dữ liệu 4 lớp** trước khi lưu (`src/v2/`): response thật hay bị chặn → từng bản
  ghi (Pydantic) → liên kết phòng/gói giá, Anh–Việt cùng ngày → chốt chặn cả đợt.
  Quy tắc lỗi/cảnh báo nằm ở `src/v2/rules.py`.
- **Crawler** lưu thêm khối `hotelDetailResponse` (sao, tọa độ, chính sách) và cào hai thứ
  tiếng cùng ngày nhận phòng. Hỗ trợ thành phố nước ngoài.
- **Đang làm:** cào lại cho v2, thử Đan Mạch; bản tiếng Anh đang bị Trip.com chặn IP (4030),
  cần nghỉ qua đêm giữa các đợt.

## Cài đặt

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt pydantic
playwright install chromium
copy .env.example .env
docker compose up -d
```

Tạo schema v2 (không đụng dữ liệu cũ):

```powershell
foreach ($f in "001_schema_v2","002_bo_sung_va_kiem_tra","003_hang_sao_circle") {
  docker cp migrations_v2\$f.sql tripcom-postgres:/tmp/$f.sql
  docker exec tripcom-postgres psql -U tripcom -d tripcom -v ON_ERROR_STOP=1 -f /tmp/$f.sql
}
```

## Lệnh chạy

**Cào + kiểm tra + nạp v2** (Việt rồi Anh, cùng ngày ở; tự bỏ qua hotel đã xong):

```powershell
python scripts\crawl_v2.py --plan                        # xem kế hoạch
python scripts\crawl_v2.py --ids 134013415               # thử 1 hotel
python scripts\crawl_v2.py --lot-size 20 --max-lots 1    # thử 1 lô
python scripts\crawl_v2.py                               # toàn bộ hotel trong DB
```

**Thành phố nước ngoài** (lấy `city`, `countryId` từ URL trang danh sách trên trip.com):

```powershell
python src\crawl_api.py --locale vi-VN --currency VND --city-id <mã> --city-name Copenhagen --country-id <mã QG> --country-name Denmark
python scripts\crawl_v2.py --list-file <output\data\api_hotels_...json>
```

**Chỉ kiểm tra / nạp raw đã có:**

```powershell
python src\db\v2_loader.py --locale vi-VN --validate-only   # báo cáo ở output\v2_reports\
python src\db\v2_loader.py --locale vi-VN                   # kiểm tra rồi nạp
python src\db\v2_loader.py --locale en-US --ids 134013415
```

**Test và web:**

```powershell
python -m unittest discover tests
python src\web_app.py            # http://127.0.0.1:8000
```

## Lưu ý

- Không chạy hai crawler cùng lúc (dùng chung browser profile).
- Bị chặn (4030 / trang đăng nhập) thì crawler tự dừng; nghỉ vài tiếng rồi chạy lại đúng lệnh cũ.
- `robots.txt` của Trip.com cấm các đường dẫn đang cào (`/hotels/list`, `/hotels/detail`,
  `/restapi/soa2`). Dữ liệu chỉ dùng nội bộ; mở rộng quy mô cần anh Vũ xác nhận.
- README cũ (lịch sử, ý tưởng, cấu trúc): xem lịch sử git hoặc `output/backup/README.truoc_v2.md`.
