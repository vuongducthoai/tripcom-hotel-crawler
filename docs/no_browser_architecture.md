# Kiến trúc No-Browser Fast HTTP Crawler (Trip.com)

Tài liệu thiết kế kỹ thuật và hướng dẫn vận hành kiến trúc thu thập dữ liệu chi tiết khách sạn Trip.com hiệu năng cao bằng phương pháp **Pure HTTP + Giả lập vân tay TLS (Chrome Impersonation) + Phân tích luồng SSR (Next.js Server Components)**, thay thế cho cơ chế điều khiển trình duyệt nặng nề (Playwright/Chromium).

---

## 1. Bối cảnh & Lý do thay đổi

Trước đây, hệ thống sử dụng Playwright (`src/crawl_detail.py`) để mở trang chi tiết khách sạn trên trình duyệt Chromium thật:
- **Tắc nghẽn bộ nhớ:** Mỗi tab trình duyệt tiêu tốn ~500 MB – 1 GB RAM. Chạy 3–4 workers đồng thời đẩy RAM lên 3–4 GB và thường xuyên gây crash Chromium (`BrowserError`, `Target closed`).
- **Tốc độ bị trần:** Thời gian render DOM, chờ selector và thực thi JavaScript mất trung bình **4.5s – 8s** cho mỗi khách sạn (chưa kể khoảng trễ an toàn giữa các request).
- **Rủi ro rớt phòng theo chuỗi:** Do phụ thuộc vào sự kiện cuộn trang (`_wait_for_room_list`) để kích hoạt tải bảng phòng qua AJAX, mạng chập chờn dễ làm sót phòng.

**Khám phá cốt lõi:**
Trip.com sử dụng Next.js với kiến trúc **React Server Components (RSC)**. Khi người dùng tải trang, toàn bộ dữ liệu khách sạn (tên, địa chỉ, ảnh, tiện ích, mô tả, chính sách và danh sách các loại phòng vật lý `physicRoomMap`) **đã được nhúng sẵn trong luồng HTML ban đầu** thông qua các khối `self.__next_f.push(...)` và JSON-LD. Không cần bất kỳ thao tác render hay chạy JavaScript nào từ phía client để lấy được những dữ liệu này.

Do đó, 1 khách sạn = **1 request HTTP thuần túy** + bóc tách cây dữ liệu JSON trong luồng SSR.

---

## 2. Sơ đồ kiến trúc tổng thể

```mermaid
flowchart TD
    Target[Danh sách Hotel ID / DB] --> Runner[src/crawl_fast.py]
    
    subgraph NetworkLayer [Tầng mạng & Vượt rào cản WAF]
        Runner --> ProxyMgr[src/engine/proxy_session.py]
        ProxyMgr --> |Direct IP| Limiter[RateLimiter: Subcritical Safe]
        ProxyMgr --> |Residential Proxy| SessRot[Session Rotation: sess_hotelId_attempt]
        Limiter --> Client[src/engine/http_client_v2.py<br>curl_cffi Chrome 124 TLS/H2]
        SessRot --> Client
        Client --> TripCom[(Trip.com Server)]
    end
    
    subgraph ValidationLayer [Tầng thẩm định ngữ nghĩa]
        TripCom --> Response[HTML Response]
        Response --> Validator[src/engine/content_validator.py]
        Validator --> |Giải mã XOR 0x0A| CheckBlock{Bị Gray-IP / CAPTCHA?}
        CheckBlock --> |Có| Retry[Exponential Backoff + Xoay Proxy mới]
        Retry --> Client
        CheckBlock --> |Không| ValidHTML[HTML hợp lệ]
    end
    
    subgraph ExtractionLayer [Tầng trích xuất không trình duyệt]
        ValidHTML --> SSRExtractor[src/ssr_extractor.py]
        SSRExtractor --> JsonLD[JSON-LD Metadata]
        SSRExtractor --> FlightChunks[Next.js Flight Chunks: self.__next_f]
        FlightChunks --> RoomMap[physicRoomMap / roomPopInfo]
        FlightChunks --> Facilities[hotelFacilityPopV2]
        SSRExtractor --> LegacyAdapter[src/detail_extract.py Synthesizer]
    end
    
    subgraph StorageLayer [Tầng lưu trữ & Nạp DB]
        LegacyAdapter --> RawStore[output/details/raw/vi-VN/VND/{id}.json.gz]
        LegacyAdapter --> Manifest[output/data/hotel_details_*.json]
        Manifest --> DBLoader[src/db/detail_loader.py]
        DBLoader --> Postgres[(PostgreSQL Database)]
    end
```

---

## 3. Các thành phần chính

### 3.1. Giả lập vân tay TLS Chrome 124 (`src/engine/http_client_v2.py`)
- Sử dụng `curl_cffi` để giả lập chính xác bộ dấu vân tay mạng của Google Chrome 124:
  - **TLS Fingerprint (JA3 / JA4):** Cấu hình Cipher suites, Extensions, Elliptic curves và Signature algorithms giống hệt trình duyệt thật.
  - **HTTP/2 Fingerprint:** Thứ tự thiết lập HTTP/2 SETTINGS frames, WINDOW_UPDATE, Header priority.
  - **Thứ tự Header HTTP:** Chuẩn hóa thứ tự và định dạng các header `Sec-Ch-Ua`, `Sec-Fetch-*`, `User-Agent`, `Accept-Language`.
- Cơ chế **Exponential Backoff with Jitter:** Tự động thử lại tối đa 5 lần với thời gian chờ tăng theo cấp số nhân và bổ sung độ lệch ngẫu nhiên.

### 3.2. Thẩm định ngữ nghĩa & Giải mã XOR Anti-bot (`src/engine/content_validator.py`)
Trip.com thường trả về mã trạng thái `200 OK` ngay cả khi yêu cầu bị chặn bởi tường lửa WAF hoặc xuất hiện thử thách xác thực bot. `content_validator.py` giải quyết triệt để vấn đề này:
- **Giải mã XOR 0x0A:** Trip.com nhúng thông tin lỗi chống bot bị mã hóa bằng thuật toán XOR với khóa `0x0A`. Validator tự động giải mã các chuỗi này để phát hiện các dấu hiệu vi phạm:
  - `failedcause: Antibot-Gray-ip` (IP bị đưa vào danh sách nghi ngờ)
  - `htlSpiderActionErrorCode` (Hệ thống phát hiện cào tự động)
- **Nhận diện CAPTCHA & Challenge:** Phát hiện trang trượt captcha (`c-slide-captcha`, `challenge_page`), trang trắng rỗng hoặc HTML không chứa luồng SSR.
- **Quyết định Retry tức thì:** Khi phát hiện challenge, response bị từ chối ngay lập tức và client kích hoạt phiên xoay proxy mới thay vì ghi nhận dữ liệu hỏng.

### 3.3. Quản lý phiên và xoay vòng Proxy (`src/engine/proxy_session.py`)
Hỗ trợ chuyển đổi liền mạch giữa các chế độ mạng:
1. **Direct IP (Mạng cục bộ):** Kích hoạt bộ điều tốc an toàn `RateLimiter` duy trì nhịp độ Subcritical Zone ($S_{eq} < 65$), tự động phân bố khoảng trễ giữa các request (1.5s – 3.5s) để bảo vệ IP máy chủ.
2. **Rotating Residential Proxy Gateway:** Hỗ trợ mẫu URL template chứa `{session_id}` (ví dụ `http://user-session-{session_id}:pass@gate.provider.com:7000`). Mỗi khách sạn và mỗi lượt retry tự động nhận một sticky IP mới.
3. **Danh sách Proxy tĩnh / Multi-port:** Đọc danh sách từ file `proxies.txt` hoặc định dạng dải cổng `{port:10001-10010}` và điều phối round-robin dựa trên băm ID khách sạn.

### 3.4. Bộ bóc tách luồng SSR không trình duyệt (`src/ssr_extractor.py`)
- Quét và giải nén toàn bộ các chuỗi chunk `self.__next_f.push([1, "..."])` của React Server Components.
- Đệ quy trích xuất cấu trúc phòng vật lý: tìm kiếm các khóa `physicRoomMap` và `roomPopInfo` nằm sâu trong payload của Next.js SSR.
- Thu thập đầy đủ album ảnh phân loại, tiện nghi tổng hợp (`hotelFacilityPopV2`), bài viết mô tả chi tiết, tọa độ và chính sách.
- Tổng hợp thành định dạng chuẩn tương thích 100% với bộ nạp cơ sở dữ liệu `src/db/detail_loader.py` mà không làm thay đổi cấu trúc bảng PostgreSQL.

---

## 4. So sánh hiệu năng thực tế

Kiểm thử đối đầu trên cùng tập mẫu khách sạn tại Hà Nội:

| Chỉ số đánh giá | Playwright Trình duyệt (`crawl_detail.py`) | No-Browser HTTP (`crawl_fast.py`) trên Direct IP | No-Browser HTTP với Residential Proxy |
| :--- | :--- | :--- | :--- |
| **Tiêu tốn RAM** | 1.5 GB – 3.5 GB (tăng dần theo thời gian) | **~70 MB phẳng** (không rò rỉ) | **~70 MB phẳng** |
| **Tải CPU** | Cao (render DOM, thực thi JS) | Cực thấp (chỉ parse chuỗi JSON) | Cực thấp |
| **Tốc độ xử lý thuần** | 4.5s – 8.0s / khách sạn | **0.5s – 0.8s / khách sạn** | **0.5s – 0.8s / khách sạn** |
| **Độ trễ chờ an toàn** | Bắt buộc 1.5s – 3.5s | Bắt buộc 1.5s – 3.5s (giữ sạch IP) | **0s (Bỏ qua hoàn toàn)** |
| **Số workers tối ưu** | 2 – 3 tabs (ngưỡng an toàn chống crash) | 2 workers | **15 – 30 workers** |
| **Tỷ lệ trích xuất phòng**| 85% – 95% (phụ thuộc vào cuộn trang) | **100%** (lấy thẳng từ SSR gốc) | **100%** |
| **Thời gian cào 100 khách sạn** | ~15 – 20 phút | ~3.5 – 4.5 phút | **~3 – 5 giây** |

---

## 5. Hướng dẫn sử dụng CLI (`src/crawl_fast.py`)

### 5.1. Chạy trên mạng trực tiếp (Direct IP)
Mặc định crawler sẽ kích hoạt chế độ `DIRECT IP (Subcritical Safe)` với 2 workers và khoảng trễ 1.5s–3.5s:
```powershell
# Cào thử 1 khách sạn theo ID
python src/crawl_fast.py --hotel-id 104981087

# Cào 30 khách sạn từ file overview
python src/crawl_fast.py --limit 30

# Cào các khách sạn còn thiếu trong Database và tự nạp vào DB
python src/crawl_fast.py --from-db --missing-only --limit 50 --apply-db
```

### 5.2. Điều chỉnh tốc độ cào trên Direct IP
Nếu đường truyền mạng cá nhân cho phép và muốn đẩy nhanh tiến độ trên Direct IP:
```powershell
python src/crawl_fast.py --limit 50 --concurrency 4 --delay-min 0.8 --delay-max 1.5
```

### 5.3. Chạy với Proxy dân cư (Residential Proxy)
Khi có proxy dân cư xoay IP (Rotating Residential Proxy):

**Cách 1: Truyền trực tiếp qua tham số dòng lệnh:**
```powershell
python src/crawl_fast.py --limit 100 --concurrency 20 --proxy "http://username-session-{session_id}:password@gate.smartproxy.com:7000"
```

**Cách 2: Cấu hình qua file `proxies.txt` hoặc `.env`:**
Dán danh sách proxy vào file `proxies.txt`:
```text
gate.proxyprovider.com:8000:user123:pass456
gate.proxyprovider.com:8001:user123:pass456
```
Và chạy:
```powershell
python src/crawl_fast.py --limit 100 --concurrency 15 --proxy proxies.txt
```

---

## 6. Kiểm thử & Đảm bảo chất lượng (QA)

Toàn bộ hệ thống được bảo vệ bởi bộ unit test hồi quy toàn diện:
```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```
- **83/83 unit tests vượt qua thành công** trong ~7.5 giây:
  - `tests/test_content_validator.py`: Kiểm thử giải mã XOR 0x0A, lọc Gray-IP, phát hiện trang thử thách.
  - `tests/test_ssr_extractor.py`: Kiểm thử trích xuất JSON-LD, parse Next.js chunks, bóc tách `physicRoomMap`.
  - 75 tests hồi quy kế thừa: Kiểm toán schema DB, chính sách khách sạn, tiện ích, mô tả và chuẩn hóa giá.
