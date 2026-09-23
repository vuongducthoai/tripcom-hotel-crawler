# Trip.com Hotel Crawler

Crawler thu thập danh sách và dữ liệu chi tiết khách sạn từ Trip.com, kiểm tra chất lượng rồi lưu vào PostgreSQL. Dữ liệu được tổ chức trong schema `v2`, hỗ trợ nhiều quốc gia, nhiều ngôn ngữ, loại phòng, gói giá, chính sách, ảnh, tiện nghi, đánh giá và địa điểm lân cận.

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
        ├─ src/crawl_detail.py      → raw từng khách sạn
        ├─ src/db/v2_loader.py      → kiểm tra dữ liệu 4 lớp
        └─ PostgreSQL schema v2     → dữ liệu đã chuẩn hóa
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
- Chromium do Playwright quản lý
- PowerShell trên Windows cho các lệnh mẫu

## Cài đặt

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt pydantic
playwright install chromium
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

Các bảng cũ trong schema `public` được tạo tự động khi volume PostgreSQL còn trống. Tạo schema mới `v2` theo đúng thứ tự:

```powershell
$migrations = @(
  "001_schema_v2",
  "002_bo_sung_va_kiem_tra",
  "003_hang_sao_circle",
  "004_tripadvisor",
  "005_so_phong"
)

foreach ($name in $migrations) {
  docker cp "migrations_v2\$name.sql" "tripcom-postgres:/tmp/$name.sql"
  docker exec tripcom-postgres psql -U tripcom -d tripcom `
    -v ON_ERROR_STOP=1 -f "/tmp/$name.sql"
}
```

Migration `001` chỉ chạy khi tạo schema lần đầu. Các migration sau bổ sung ràng buộc, theo dõi lần nạp, Tripadvisor và số phòng khách sạn.

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

Không commit `.env`, cookie trình duyệt hoặc thông tin proxy.

### Browser profile

Tạo profile tiếng Việt trước lần crawl đầu tiên:

```powershell
python src\setup_profile.py --locale vi-VN --currency VND
```

Trong cửa sổ Chromium:

1. Đăng nhập Trip.com nếu có tài khoản.
2. Chọn tiếng Việt và VND.
3. Tìm thử thành phố cần crawl và mở trang kết quả.
4. Xử lý captcha hoặc banner nếu xuất hiện.
5. Đóng toàn bộ cửa sổ Chromium để lưu profile.

Mỗi thị trường dùng profile riêng. Nếu sau này crawl tiếng Anh:

```powershell
python src\setup_profile.py --locale en-US --currency USD
```

## Quy trình crawl tiếng Việt

### 1. Lấy danh sách khách sạn

Ví dụ Copenhagen:

```powershell
python src\crawl_api.py `
  --locale vi-VN --currency VND `
  --city-id 260 --city-name Copenhagen `
  --country-id 27 --country-name Denmark
```

Kết quả nằm tại `output/data/api_hotels_260_viVN_VND_<timestamp>.json`. File có các trường `count`, `city_total_reported` và `complete`. Nếu `complete=false`, Trip.com đã dừng phân trang sớm hoặc số ID chưa đạt ngưỡng hoàn chỉnh.

Chọn tự động file Copenhagen mới nhất để dùng cho các bước tiếp theo:

```powershell
$listFile = (Get-ChildItem output\data\api_hotels_260_viVN_VND_*.json |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1).FullName
```

Chạy thử nhanh trước khi lấy toàn thành phố:

```powershell
python src\crawl_api.py `
  --locale vi-VN --currency VND `
  --city-id 260 --city-name Copenhagen `
  --country-id 27 --country-name Denmark `
  --max-pages 1 --limit 20
```

### 2. Xem kế hoạch crawl chi tiết

```powershell
python scripts\crawl_v2.py `
  --list-file $listFile `
  --only vi --plan
```

### 3. Thử một lô nhỏ

```powershell
python scripts\crawl_v2.py `
  --list-file $listFile `
  --only vi --workers 1 --lot-size 5 --max-lots 1
```

Lệnh này thực hiện đủ ba bước: crawl detail, kiểm tra dữ liệu và nạp schema `v2`.

### 4. Chạy toàn bộ danh sách

```powershell
python scripts\crawl_v2.py `
  --list-file $listFile `
  --only vi --workers 2 --lot-size 50
```

Nếu lệnh dừng do bị chặn hoặc mất kết nối, chạy lại đúng lệnh cũ. Raw đã hoàn thành được nhận diện và bỏ qua tự động.

### Các cách chọn phạm vi khác

```powershell
# Một hoặc vài khách sạn
python scripts\crawl_v2.py --ids 134013415 110585808 --only vi

# Một thành phố đã có trong database cũ
python scripts\crawl_v2.py --city-id 1356 --only vi

# Toàn bộ khách sạn trong database cũ
python scripts\crawl_v2.py --only vi

# Crawl lại khách sạn đã hoàn thành
python scripts\crawl_v2.py --ids 134013415 --only vi --redo
```

## Crawl chi tiết trực tiếp

```powershell
python src\crawl_detail.py `
  --file $listFile `
  --locale vi-VN --currency VND `
  --limit 1 --workers 1
```

Tùy chọn hữu ích:

- `--ids-file`: chỉ crawl các ID trong file, mỗi dòng một ID.
- `--checkin` và `--checkout`: ngày ở dạng `YYYY-MM-DD`.
- `--no-resume`: bỏ cache và crawl lại.
- `--require-detail-block`: chỉ chấp nhận raw có `hotelDetailResponse`.
- `--browser-channel chrome`: dùng Chrome hệ thống thay cho Chromium của Playwright.

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

Không dùng `--force` nếu chưa xem báo cáo và raw gây lỗi.

## Proxy

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

Kiểm tra trước khi crawl:

```powershell
python scripts\test_proxy.py
```

Script kiểm tra cả `httpx` và Chromium. Proxy trả HTTP 200 ở trang chủ chưa đảm bảo API trang chi tiết hoạt động ổn định. Luôn thử một khách sạn với `--limit 1`; nếu gặp `ERR_INVALID_AUTH_CREDENTIALS`, `ERR_TUNNEL_CONNECTION_FAILED`, timeout hoặc raw không có phòng, đặt lại:

```dotenv
TRIP_PROXY_ENABLED=false
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

## Giao diện dữ liệu cục bộ

```powershell
python src\web_app.py
```

Mở [http://127.0.0.1:8000](http://127.0.0.1:8000). Giao diện đọc PostgreSQL ở chế độ read-only và có trang theo dõi crawl.

PgAdmin chạy tại [http://localhost:5050](http://localhost:5050) với tài khoản mặc định trong `docker-compose.yaml`.

## Tripadvisor tùy chọn

Migration `004_tripadvisor.sql` và `scripts/tripadvisor_match.py` dùng Tripadvisor Content API chính thức để ghép điểm, số đánh giá và URL với khách sạn trong `v2`.

```powershell
$env:TRIPADVISOR_API_KEY="<api-key>"
python scripts\tripadvisor_match.py --limit 5 --dry-run
```

Kết quả cần duyệt nằm trong `output/tripadvisor/`. Việc sử dụng và lưu dữ liệu phải tuân theo điều khoản cùng chính sách cache của Tripadvisor.

## Kiểm thử

```powershell
python -m unittest discover tests
python -m py_compile src\crawl_api.py src\crawl_detail.py scripts\crawl_v2.py
```

## Xử lý sự cố

### `htlSpiderActionErrorCode=4030` hoặc antibot

- Dừng crawler thay vì tiếp tục gửi request.
- Đóng Chromium và chờ vài giờ trước khi chạy lại.
- Nếu profile hết phiên, chạy lại `setup_profile.py`, đăng nhập và tìm thử thành phố.
- Giảm `--workers` xuống `1` và tăng thời gian nghỉ giữa các lô.

### Không mở được browser profile

Chỉ một tiến trình Chromium được dùng cùng một profile. Dừng `crawl_api.py`, `crawl_detail.py`, `setup_profile.py` và các cửa sổ Chromium liên quan rồi chạy lại.

### `THIẾU PHÒNG`, ảnh và tiện nghi đều bằng 0

Đây thường là response không đầy đủ, proxy lỗi hoặc phiên bị giới hạn. Không xem bản ghi đó là crawl thành công. Kiểm tra raw `.failed.*`, thử một khách sạn bằng kết nối trực tiếp và chỉ chạy tiếp khi dữ liệu phòng xuất hiện.

### File danh sách có `complete=false`

Trip.com có thể báo hết trang sớm. Crawler thử vét theo khoảng giá nhưng vẫn giữ `complete=false` nếu chưa đạt `MIN_COMPLETE_RATIO` trong `.env`.

### Chạy lại sau khi bị dừng

Dùng đúng lệnh trước đó. Checkpoint, raw thành công và trạng thái nạp giúp crawler tiếp tục phần còn thiếu.

## Nguyên tắc vận hành

- Không chạy nhiều crawler cùng lúc trên cùng browser profile.
- Bắt đầu bằng `--limit 1` hoặc một lô nhỏ trước khi chạy toàn bộ thành phố.
- Không ghi thông tin đăng nhập, proxy hoặc API key vào Git và log chia sẻ.
- Giữ tốc độ thấp; crawler tự dừng khi lỗi liên tiếp hoặc chốt chất lượng không đạt.
- Tuân thủ điều khoản sử dụng, `robots.txt` và phạm vi sử dụng dữ liệu đã được phê duyệt.

## Tài liệu liên quan

- [Thiết kế schema v2](docs/schema_v2.md)
- [Ghi chú recon và phân trang](docs/recon.md)
- [Migration schema v2](migrations_v2/)
- [Quy tắc kiểm tra dữ liệu](src/v2/rules.py)
