# Trip.com Hotel Crawler

Crawler thu thập danh sách và dữ liệu chi tiết khách sạn từ Trip.com, kiểm tra chất lượng rồi lưu vào PostgreSQL. Dữ liệu được tổ chức trong schema `v2`, hỗ trợ nhiều quốc gia, nhiều ngôn ngữ, loại phòng, gói giá, chính sách, ảnh, tiện nghi, đánh giá và địa điểm lân cận.

## Hiện trạng

- **Dữ liệu cũ (schema `public`):** 3.431 khách sạn TP.HCM, giữ nguyên để tham khảo.
- **Schema mới `v2`** ([docs/schema_v2.md](docs/schema_v2.md)): song ngữ, đa quốc gia, tách loại phòng / gói giá, chính sách có cấu trúc. Migration `migrations_v2/001` → `006` (35 bảng).
- **Kiểm tra dữ liệu 4 lớp** trước khi lưu (`src/v2/`): response thật hay bị chặn → từng bản ghi (Pydantic) → liên kết phòng/gói giá, Anh–Việt cùng ngày → chốt chặn cả đợt. Quy tắc nằm ở `src/v2/rules.py`.
- **Crawler Playwright & No-Browser** lưu đầy đủ khối `hotelDetailResponse` (hạng sao, sao/circle, tọa độ, chính sách chuẩn) và cào hai thứ tiếng cùng ngày nhận phòng (`default_stay` / `paired_stay`).
- **Kiến trúc No-Browser Fast HTTP Crawler** (`src/crawl_fast.py`, `src/engine/`): giả lập TLS Chrome 124 qua `curl_cffi`, bóc tách trực tiếp React Server Components SSR không cần mở Chromium, giảm RAM xuống ~70MB phẳng và tăng tốc 5–10x ([docs/no_browser_architecture.md](docs/no_browser_architecture.md)). Hỗ trợ cả runner HTTPX với chế độ `--chi-dump` phục vụ bảng dịch thuật của mentor.

## Luồng dữ liệu

```text
Trip.com trang danh sách
        │
        ▼
src/crawl_api.py
        │  output/data/api_hotels_*.json
        ▼
scripts/crawl_v2.py --only vi
        │
        ├─ src/crawl_fast.py / src/crawl_detail.py → raw từng khách sạn (gzip)
        ├─ src/db/v2_loader.py                     → kiểm tra dữ liệu 4 lớp
        └─ PostgreSQL schema v2                    → dữ liệu đã chuẩn hóa
```

Crawler có checkpoint và cache raw. Khi chạy lại cùng phạm vi, các khách sạn đã hoàn thành được bỏ qua, trừ khi dùng `--redo` hoặc tùy chọn buộc crawl lại.

## Dữ liệu được lưu

| Nhóm | Nội dung |
|---|---|
| Địa lý | Quốc gia, thành phố, tỉnh, tọa độ và múi giờ |
| Khách sạn | Tên, địa chỉ, hạng sao, năm mở cửa, số phòng và mô tả |
| Ảnh | Album, danh mục ảnh, ảnh bìa, ảnh khách sạn và ảnh khách đăng |
| Tiện nghi | Danh mục theo mã Trip.com, trạng thái, phí và chi tiết |
| Chính sách | Nhận/trả phòng, trẻ em, giường phụ, bữa sáng, đặt cọc, thú cưng và thanh toán |
| Phòng | Loại phòng, diện tích, giường, sức chứa, hướng nhìn và tiện nghi |
| Gói giá | Giá theo ngày ở và tiền tệ, bữa ăn, hủy phòng và hình thức thanh toán |
| Đánh giá | Điểm tổng, điểm thành phần, số đánh giá và nhãn nhận xét |
| Lân cận | Giao thông, điểm tham quan, khoảng cách và cách di chuyển |
| Theo dõi | Lịch sử crawl, raw nguồn, phiên bản parser và lỗi bị chặn |

Chi tiết thiết kế nằm tại [docs/schema_v2.md](docs/schema_v2.md).

## Yêu cầu

- Python 3.11 trở lên
- Docker Desktop và Docker Compose
- PostgreSQL 16 qua `docker-compose.yaml`
- Chromium do Playwright quản lý (cho crawl Playwright hoặc lấy cookie)
- PowerShell trên Windows cho các lệnh mẫu

## Cài đặt

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

Khởi tạo schema mới `v2` theo đúng thứ tự:

```powershell
$migrations = @(
  "001_schema_v2",
  "002_bo_sung_va_kiem_tra",
  "003_hang_sao_circle",
  "004_tripadvisor",
  "005_so_phong",
  "006_nhom_lan_can_theo_ngon_ngu"
)

foreach ($name in $migrations) {
  docker cp "migrations_v2\$name.sql" "tripcom-postgres:/tmp/$name.sql"
  docker exec tripcom-postgres psql -U tripcom -d tripcom `
    -v ON_ERROR_STOP=1 -f "/tmp/$name.sql"
}
```

Nếu muốn khởi tạo schema `public` cũ (chỉ cần nếu dựng DB mới tinh từ đầu):

```powershell
docker exec -i tripcom-postgres psql -U tripcom -d tripcom < migrations/001_init.sql
```

## Cấu hình `.env`

```dotenv
HEADLESS=false
LOCALE=vi-VN
CURRENCY=VND
TIMEZONE=Asia/Ho_Chi_Minh
PAGE_TIMEOUT_MS=60000

DB_HOST=localhost
DB_PORT=5433
DB_USER=tripcom
DB_PASSWORD=tripcom
DB_NAME=tripcom
```

### Proxy

Proxy mặc định tắt. Có thể dán nguyên chuỗi `host:port:user:password` vào `.env`:

```dotenv
TRIP_PROXY_ENABLED=true
TRIP_PROXY=proxy.example.net:46032:username:password
```

Hoặc cấu hình từng trường:

```dotenv
TRIP_PROXY_ENABLED=true
TRIP_PROXY_SERVER=http://proxy.example.net:46032
TRIP_PROXY_USERNAME=username
TRIP_PROXY_PASSWORD=password
```

Kiểm tra kết nối proxy trước khi crawl:

```powershell
python scripts\test_proxy.py
```

### Browser profile

Tạo profile tiếng Việt trước lần crawl đầu tiên nếu cần dùng browser automation:

```powershell
python src\setup_profile.py --locale vi-VN --currency VND
python src\setup_profile.py --locale en-US --currency USD
```

## Quy trình crawl dữ liệu

### 1. Lấy danh sách khách sạn

Ví dụ thành phố Copenhagen:

```powershell
python src\crawl_api.py `
  --locale vi-VN --currency VND `
  --city-id 260 --city-name Copenhagen `
  --country-id 27 --country-name Denmark
```

File lưu tại `output/data/api_hotels_260_viVN_VND_<timestamp>.json`. Tự động lấy file mới nhất:

```powershell
$listFile = (Get-ChildItem output\data\api_hotels_260_viVN_VND_*.json |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1).FullName
```

### 2. Fast HTTP Crawler (No-Browser — Tốc độ cao)

Sử dụng `curl_cffi` giả lập vân tay TLS Chrome 124 hoặc HTTPX client, bóc tách luồng Next.js SSR trực tiếp:
- **Tốc độ:** ~0.5s–0.8s/khách sạn.
- **Bộ nhớ:** ~70 MB RAM phẳng (so với 2–3 GB của Chromium).
- **Đầy đủ dữ liệu:** Bóc tách 100% ảnh, tiện ích, mô tả, cấu trúc phòng (`physicRoomMap`), chính sách và địa điểm lân cận tương thích Schema V2.
- **Tài liệu kiến trúc:** [`docs/no_browser_architecture.md`](docs/no_browser_architecture.md).

```powershell
# Cào thử 1 khách sạn theo ID
python src\crawl_fast.py --hotel-id 104981087

# Cào danh sách với chế độ Direct IP an toàn (2 workers)
python src\crawl_fast.py --limit 30

# Cào các khách sạn còn thiếu trong DB và tự động nạp
python src\crawl_fast.py --from-db --missing-only --apply-db

# Chạy với Residential Proxy xoay tua (15 workers)
python src\crawl_fast.py --limit 100 --concurrency 15 --proxy proxies.txt

# Chế độ chỉ dump (mô tả, chính sách, lân cận) cho mentor
python src\crawl_fast.py --ids-file output\ids.txt --chi-dump
```

### 3. Điều phối cào theo lô song ngữ

```powershell
# Xem kế hoạch crawl
python scripts\crawl_v2.py --list-file $listFile --only vi --plan

# Thử một lô nhỏ
python scripts\crawl_v2.py --list-file $listFile --only vi --workers 1 --lot-size 5 --max-lots 1

# Chạy crawler nhanh (No-Browser) cho cả danh sách
python scripts\crawl_v2.py --list-file $listFile --fast --only vi --workers 2 --lot-size 50

# Chế độ chỉ lấy dữ liệu cho bảng dump của mentor
python scripts\crawl_v2.py --ids-file output\ids.txt --fast --chi-dump
```

### 4. Playwright Crawler (Browser Automation — Dự phòng)

Sử dụng Chromium qua Playwright (`src/crawl_detail.py`) khi cần kiểm tra trực quan hoặc đối soát:

```powershell
python src\crawl_detail.py --file $listFile --locale vi-VN --currency VND --limit 1 --workers 1
```

## Kiểm tra và nạp raw có sẵn

```powershell
# Chỉ kiểm tra, không ghi PostgreSQL
python src\db\v2_loader.py --locale vi-VN --validate-only

# Kiểm tra rồi nạp
python src\db\v2_loader.py --locale vi-VN

# Giới hạn phạm vi
python src\db\v2_loader.py --locale vi-VN --ids 134013415
python src\db\v2_loader.py --locale vi-VN --ids-file output\batches_v2\<batch>.txt
```

Báo cáo nằm trong `output/v2_reports/`. Loader kiểm tra bốn lớp:
1. Response có phải dữ liệu thật hay bị 4030, đăng nhập hoặc antibot.
2. Kiểu dữ liệu, miền giá trị và trường bắt buộc.
3. Liên kết giữa khách sạn, phòng, gói giá và bản dịch.
4. Tỷ lệ lỗi của cả đợt so với các ngưỡng an toàn.

## Bảng dịch thuật Mentor (`trip_tmp_property_translation`)

Phục vụ việc bàn giao dữ liệu dịch thuật song ngữ theo yêu cầu mentor:
- Cấu trúc: `row_uuid | property_id | type | section_type | lang | field | value`.
- Hỗ trợ các mục: `DESCRIPTION`, `POLICY`, `SURROUNDING`.
- Script trích xuất: `scripts/export_trip_property_translation.sql`.
- Dữ liệu dump mẫu: `dump_translation.sql`, `dump_translation_insert.txt`.
- Ghi chú chi tiết: [`docs/ghi_chu_mentor.md`](docs/ghi_chu_mentor.md).

## Giao diện dữ liệu cục bộ

```powershell
python src\web_app.py
```

Mở [http://127.0.0.1:8000](http://127.0.0.1:8000). Giao diện đọc PostgreSQL ở chế độ read-only và có trang theo dõi crawl.
PgAdmin chạy tại [http://localhost:5050](http://localhost:5050).

## Tripadvisor tùy chọn

Migration `004_tripadvisor.sql` và `scripts/tripadvisor_match.py` dùng Tripadvisor Content API để ghép điểm, số đánh giá và URL với khách sạn trong `v2`:

```powershell
$env:TRIPADVISOR_API_KEY="<api-key>"
python scripts\tripadvisor_match.py --limit 5 --dry-run
```

## File và thư mục đầu ra

| Đường dẫn | Nội dung |
|---|---|
| `output/data/api_hotels_*.json` | Danh sách khách sạn theo thành phố |
| `output/data/hotel_details_*.json` | Manifest của một lượt crawl detail |
| `output/details/raw/<locale>/<currency>/` | Raw từng khách sạn, thường nén `.json.gz` |
| `output/batches_v2/` | Danh sách ID của từng lô |
| `output/v2_reports/` | Báo cáo kiểm tra trước khi nạp |
| `output/html/` | HTML và ảnh debug trang danh sách |
| `output/recon/` | Request/response phục vụ phân tích endpoint |

## Cấu trúc thư mục

```
src/v2/                  bộ thẩm định & bóc tách dữ liệu 4 lớp (rules, models, extract, writer)
src/db/v2_loader.py      nạp raw JSON vào schema v2 với báo cáo audit (output/v2_reports/)
migrations_v2/           schema v2: 001_schema_v2 → 006_nhom_lan_can_theo_ngon_ngu
scripts/crawl_v2.py      điều phối cào theo lô song ngữ đồng bộ ngày (gọi subprocess)
scripts/find_missing.py  tìm khách sạn thiếu raw hoặc thiếu khối v2
src/crawl_fast.py        crawler chi tiết No-Browser siêu tốc (dual engine: curl_cffi & httpx)
src/ssr_extractor.py     bóc tách luồng Next.js React Server Components (physicRoomMap, detail, policy, nearby)
src/engine/              hệ thống mạng: giả lập TLS Chrome 124, giải mã XOR chống bot, xoay proxy
src/block_detect.py      nhận diện antibot 4030, captcha, redirect đăng nhập
src/fast_api.py          tiện ích mẫu API cho cào enrichment tĩnh
src/config.py            cấu hình tập trung, đọc từ .env
src/setup_profile.py     tạo Chromium profile dùng lại (chạy 1 lần)
src/crawl_api.py         pipeline danh sách: SSR trang 1 + bắt/phát lại API phân trang
src/crawl_detail.py      crawler chi tiết trình duyệt (Playwright, phương án dự phòng)
src/web_app.py           giao diện web tra cứu DB read-only và theo dõi crawler
```

## Kiểm thử

```powershell
python -m unittest discover tests
python -m py_compile src\crawl_api.py src\crawl_detail.py scripts\crawl_v2.py src\crawl_fast.py
```

## Xử lý sự cố

### `htlSpiderActionErrorCode=4030` hoặc antibot

- Dừng crawler thay vì tiếp tục gửi request.
- Đóng Chromium và chờ vài giờ trước khi chạy lại, hoặc chuyển sang dùng proxy dân cư xoay tua.
- Nếu profile hết phiên, chạy lại `setup_profile.py`, đăng nhập và tìm thử thành phố.
- Giảm `--concurrency` xuống `1`–`2` và tăng thời gian nghỉ giữa các lượt.

### Không mở được browser profile

Chỉ một tiến trình Chromium được dùng cùng một profile. Dừng `crawl_api.py`, `crawl_detail.py`, `setup_profile.py` và các cửa sổ Chromium liên quan rồi chạy lại.

### `THIẾU PHÒNG`, ảnh và tiện nghi đều bằng 0

Đây thường là response không đầy đủ, proxy lỗi hoặc phiên bị giới hạn. Không xem bản ghi đó là crawl thành công. Kiểm tra raw `.failed.*`, thử một khách sạn bằng kết nối trực tiếp và chỉ chạy tiếp khi dữ liệu xuất hiện.

## Nguyên tắc vận hành

- Không chạy nhiều crawler cùng lúc trên cùng browser profile.
- Bắt đầu bằng `--limit 1` hoặc một lô nhỏ trước khi chạy toàn bộ thành phố.
- Không ghi thông tin đăng nhập, proxy hoặc API key vào Git và log chia sẻ.
- Giữ tốc độ an toàn; crawler tự dừng khi lỗi liên tiếp hoặc chốt chất lượng không đạt.
- Tuân thủ điều khoản sử dụng, `robots.txt` và phạm vi sử dụng dữ liệu đã được phê duyệt.

## Tài liệu liên quan

- [Thiết kế schema v2](docs/schema_v2.md)
- [Kiến trúc No-Browser Fast HTTP Crawler](docs/no_browser_architecture.md)
- [Báo cáo mô phỏng Rate Limit](docs/rate_limit_parameters_report.md)
- [Ghi chú thống nhất với Mentor](docs/ghi_chu_mentor.md)
- [Migration schema v2](migrations_v2/)
- [Quy tắc kiểm tra dữ liệu](src/v2/rules.py)
