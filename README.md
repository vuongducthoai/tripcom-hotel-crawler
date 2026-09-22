# Trip.com Hotel Crawler

Crawl dữ liệu khách sạn Trip.com (tiếng Việt + tiếng Anh) về PostgreSQL.

- **Người thực hiện:** Vương Đức Thoại, Trần Đăng Nguyên (S.AI20K)
- **Nghiệm thu:** Nguyễn Thạch Vũ (VSF-KD&VH DLKS-PMKD)

## Hiện trạng

- **Dữ liệu cũ (schema `public`):** 3.431 khách sạn TP.HCM, giữ nguyên để tham khảo.
- **Schema mới `v2`** ([docs/schema_v2.md](docs/schema_v2.md)): song ngữ, đa quốc gia, tách
  loại phòng / gói giá, chính sách có cấu trúc. Migration `migrations_v2/001` → `003` (35 bảng).
- **Kiểm tra dữ liệu 4 lớp** trước khi lưu (`src/v2/`): response thật hay bị chặn → từng bản
  ghi (Pydantic) → liên kết phòng/gói giá, Anh–Việt cùng ngày → chốt chặn cả đợt.
  Quy tắc lỗi/cảnh báo nằm ở `src/v2/rules.py`.
- **Crawler Playwright & No-Browser** lưu đầy đủ khối `hotelDetailResponse` (hạng sao, sao/circle, tọa độ, chính sách chuẩn) và cào hai thứ tiếng cùng ngày nhận phòng (`default_stay` / `paired_stay`). Hỗ trợ thành phố quốc tế.
- **Kiến trúc No-Browser Fast HTTP Crawler** (`src/crawl_fast.py`, `src/engine/`): giả lập TLS Chrome 124 qua `curl_cffi`, bóc tách trực tiếp React Server Components SSR không cần mở Chromium, giảm RAM xuống ~70MB phẳng và tăng tốc 5–10x ([docs/no_browser_architecture.md](docs/no_browser_architecture.md)).
- **Đang làm:** cào lại cho v2, thử Đan Mạch; bản tiếng Anh đang bị Trip.com chặn IP (4030),
  cần nghỉ qua đêm giữa các đợt hoặc dùng proxy xoay tua.

## Cài đặt

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
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

Nếu muốn khởi tạo schema `public` cũ (chỉ cần chạy nếu dựng DB mới tinh từ đầu):

```powershell
docker exec -i tripcom-postgres psql -U tripcom -d tripcom < migrations/001_init.sql
```

## Lệnh chạy

### 1. Cào + kiểm tra + nạp Schema V2 (Khuyến nghị cho quy trình V2)

Cào tự động theo lô (Việt rồi Anh, cùng ngày ở, tự bỏ qua hotel đã xong):

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

**Chỉ kiểm tra / nạp raw đã có vào V2:**

```powershell
python src\db\v2_loader.py --locale vi-VN --validate-only   # báo cáo ở output\v2_reports\
python src\db\v2_loader.py --locale vi-VN                   # kiểm tra rồi nạp
python src\db\v2_loader.py --locale en-US --ids 134013415
```

### 2. Fast HTTP Crawler (No-Browser — Tốc độ cao)

Sử dụng `curl_cffi` giả lập vân tay TLS Chrome 124, bóc tách luồng Next.js SSR mà **không cần mở trình duyệt Chromium**:
- **Tốc độ:** ~0.6s/khách sạn (nhanh gấp 5–10 lần trình duyệt).
- **Bộ nhớ:** ~70 MB RAM phẳng (so với 2–3 GB của Playwright).
- **Đầy đủ dữ liệu:** Bóc tách 100% ảnh, tiện ích, mô tả, cấu trúc phòng (`physicRoomMap`) và khối `hotelDetailResponse` cho Schema V2.
- **Tài liệu kiến trúc chi tiết:** Xem [`docs/no_browser_architecture.md`](docs/no_browser_architecture.md).

```powershell
# Cào thử 1 khách sạn theo ID
python src\crawl_fast.py --hotel-id 104981087

# Cào 30 khách sạn trên Direct IP (an toàn, 2 workers, delay 1.5s-3.5s)
python src\crawl_fast.py --limit 30

# Cào các khách sạn còn thiếu trong DB
python src\crawl_fast.py --from-db --missing-only --limit 50

# Chạy với Residential Proxy xoay tua (15 workers)
python src\crawl_fast.py --limit 100 --concurrency 15 --proxy proxies.txt
```

### 3. Playwright Crawler (Browser Automation — Dự phòng)

Sử dụng trình duyệt Chromium thật qua Playwright (`src/crawl_detail.py`). Giữ lại làm phương án dự phòng khi cần:

```powershell
# Tạo browser profile một lần duy nhất (nếu chưa có):
python src\setup_profile.py

# Cào danh sách chi tiết:
python src\crawl_detail.py --file api_hotels_301_xxx.json --limit 1
python src\crawl_detail.py --from-db --workers 2
```

### 4. Web Demo & Database Inspector

Web demo chạy cục bộ, trang chính hiển thị danh sách khách sạn đã crawl và cho phép mở
chi tiết theo từng phân mục: tổng quan, VI/EN, phòng, giá, ảnh, tiện nghi, chính sách,
vị trí lân cận và raw JSON. Trang `/database.html` là Database Inspector, tự đọc schema
để kiểm tra bảng/cột, lọc `NULL`, sắp xếp và xuất JSON. Mọi kết nối của
web đều ở chế độ **read-only**, không sửa dữ liệu và có thể chạy cùng lúc với crawler.

```powershell
python src\web_app.py            # Mở http://127.0.0.1:8000 hoặc /database.html
```

### 5. Kiểm thử (Unit Tests)

```powershell
python -m unittest discover tests
```

## Cấu trúc thư mục

```
src/v2/                  bộ thẩm định & bóc tách dữ liệu 4 lớp (rules, models, extract, writer)
src/db/v2_loader.py      nạp raw JSON vào schema v2 với báo cáo audit (output/v2_reports/)
migrations_v2/           schema v2: 001_schema_v2, 002_bo_sung_va_kiem_tra, 003_hang_sao_circle
scripts/crawl_v2.py      điều phối cào theo lô song ngữ đồng bộ ngày (gọi subprocess)
scripts/find_missing.py  tìm khách sạn thiếu raw hoặc thiếu khối v2
src/crawl_fast.py        crawler chi tiết No-Browser siêu tốc (HTTP + curl_cffi Chrome 124)
src/ssr_extractor.py     bóc tách luồng Next.js React Server Components (physicRoomMap, detail)
src/engine/              hệ thống mạng v2: giả lập TLS, kiểm tra XOR chống bot, xoay proxy
src/config.py            cấu hình tập trung, đọc từ .env
src/setup_profile.py     tạo Chromium profile dùng lại (chạy 1 lần)
src/crawl_api.py         pipeline danh sách: SSR trang 1 + bắt/phát lại API phân trang (hỗ trợ quốc tế)
src/api_extract.py       parse response API + HTML SSR → dict khớp cột DB
src/crawl_detail.py      crawler chi tiết trình duyệt (Playwright, phương án dự phòng)
src/detail_extract.py    parse response trang chi tiết (tổng hợp dữ liệu chuẩn)
src/db/loader.py         upsert danh sách khách sạn vào PostgreSQL (schema public)
src/db/detail_loader.py  upsert detail + location + loại phòng + giá theo ngày (schema public)
migrations/001_init.sql  schema public cũ: hotels, locations, hotel_images, hotel_amenities...
```

## Lưu ý

- Không chạy hai crawler Chromium cùng lúc (dùng chung browser profile).
- Bị chặn (4030 / trang đăng nhập) thì crawler tự dừng; nghỉ vài tiếng rồi chạy lại đúng lệnh cũ.
- `robots.txt` của Trip.com cấm các đường dẫn đang cào (`/hotels/list`, `/hotels/detail`, `/restapi/soa2`). Dữ liệu chỉ dùng nội bộ; mở rộng quy mô cần anh Vũ xác nhận.
- README cũ (lịch sử, ý tưởng, cấu trúc ban đầu): xem lịch sử git hoặc `output/backup/README.truoc_v2.md`.
