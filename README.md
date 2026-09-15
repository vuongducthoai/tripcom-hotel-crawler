# Trip.com Hotel Crawler

Crawl dữ liệu khách sạn từ Trip.com về PostgreSQL.

- **Người thực hiện:** Vương Đức Thoại, Trịnh Quang Anh (S.AI20K)
- **Nghiệm thu:** Đặng Nguyễn Quyết Thắng (VSF-KD&VH DLKS-PMKD)

## Ý tưởng

Có hai đường lấy dữ liệu, và chúng khác nhau rất xa về tốc độ:

| | Đường A — API JSON | Đường B — render HTML |
|---|---|---|
| Công cụ | `httpx` (`src/http_client.py`) | Crawl4AI + Chromium (`src/crawl_list.py`) |
| Tốc độ | nhanh, không mở browser | chậm hơn 5–10 lần |
| Ổn định | cao | phụ thuộc CSS, gãy khi site đổi layout |

**Luôn thử đường A trước.** `src/recon.py` tồn tại chỉ để trả lời một câu hỏi:
Trip.com có API JSON gọi thẳng được không? Chỉ khi câu trả lời là *không* thì
mới dùng đường B.

## Cài đặt

Cần Python 3.10+ và Docker Desktop.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium
copy .env.example .env           # rồi mở ra sửa
docker compose up -d             # Postgres + pgAdmin (localhost:5050)
```

`migrations/001_init.sql` chạy tự động lần đầu container khởi tạo. Nếu đã có
volume cũ thì chạy tay:

```bash
docker exec -i tripcom-postgres psql -U tripcom -d tripcom < migrations/001_init.sql
```

## Chạy

### 1. Tạo browser profile (một lần duy nhất)

```bash
python src/setup_profile.py
```

Cửa sổ Chromium mở ra. Tự tay: chọn tiếng Việt + VND, tắt popup, giải captcha
nếu có, search thử một thành phố. **Đóng cửa sổ để lưu.** Cookie và fingerprint
nằm ở `browser_profile/` và mọi script sau dùng lại — đây là thứ giúp không bị
chặn, khác hẳn headless context trắng.

### 2. Recon — tìm API JSON

```bash
python src/recon.py
python src/recon.py --url "https://vn.trip.com/hotels/list?city=359"
```

Đọc `output/recon/summary.md`. Bảng sắp theo kích thước response giảm dần;
endpoint to nhất thường chính là endpoint trả danh sách khách sạn. Mở vài file
trong `output/recon/bodies/` để xác nhận.

- **Tìm thấy** → ghi endpoint + params phân trang vào `docs/recon.md`, rồi viết
  crawler dùng `src/http_client.py`. Bỏ qua bước 3.
- **Không thấy** → sang bước 3.

### 3. Crawl bằng HTML (phương án B)

```bash
python src/crawl_list.py --probe            # thử selector, chưa lưu
python src/crawl_list.py --schema class-contains-card
```

`--probe` thử từng bộ selector trong `src/hotel_selectors.py` và in ra bộ nào bắt
được nhiều card nhất. Selector của Trip.com là class hash sinh tự động nên
**chắc chắn sẽ phải sửa tay** — mở file HTML trong `output/html/` bằng Chrome,
Inspect một card, rồi cập nhật `baseSelector`.

Chạy lại offline không cần mở browser:

```bash
python src/crawl_list.py --from-file output/html/xxx.html --probe
```

### 4. Nạp vào PostgreSQL

```bash
python src/db/loader.py                 # file mới nhất
python src/db/loader.py --cheap         # đánh dấu is_cheap_listing
```

Upsert theo `trip_hotel_id`, chạy lại bao nhiêu lần cũng không nhân đôi dữ liệu.

## Cấu trúc

```
src/config.py         cấu hình tập trung, đọc từ .env
src/setup_profile.py  tạo Chromium profile dùng lại
src/recon.py          bắt XHR/fetch → tìm API JSON      ← quan trọng nhất
src/http_client.py    httpx + rate limit + retry (đường A)
src/crawl_list.py     Crawl4AI + cuộn + CSS (đường B)
src/hotel_selectors.py      bộ selector ứng viên, sửa tay sau khi probe
src/extract.py        parse HTML bằng bs4, không dùng LLM
src/db/loader.py      upsert vào PostgreSQL
migrations/001_init.sql
```

## Nguyên tắc đã áp dụng

- **Không dùng LLM để extract.** Vài nghìn khách sạn qua GPT là tốn tiền vô lý
  và kết quả đổi giữa các lần chạy. CSS selector miễn phí và deterministic.
- **Giữ `raw_json`.** Mỗi bản ghi lưu nguyên bản. Khi Trip.com đổi layout hoặc
  sếp hỏi field mới, parse lại từ dữ liệu cũ thay vì crawl lại từ đầu.
- **Upsert theo natural key**, không insert mù.
- **Không nuốt lỗi.** Mọi request hỏng ghi vào `crawl_errors` rồi đi tiếp.
- **Chạy chậm có chủ đích.** `MIN_DELAY`/`MAX_CONCURRENCY` để thấp. Bị block
  một lần là mất cả buổi để gỡ.

## Còn thiếu

- Phân trang qua nhiều trang danh sách (hiện mới cuộn trong một trang)
- Crawl trang chi tiết: ảnh, tiện ích, loại phòng
- Giá theo ngày check-in (`hotel_prices`) — chờ anh Thắng chốt có cần không
- Checkpoint/resume cho lần chạy dài
- Cây địa điểm (`locations`) — hiện chưa crawl

## Lưu ý pháp lý

`RESPECT_ROBOTS=true` là mặc định. Nếu `robots.txt` của Trip.com chặn đường dẫn
khách sạn, **đó là câu hỏi đưa lên anh Thắng**, không phải thứ tự tắt cờ đi cho
xong. Dữ liệu dùng nội bộ/demo, không redistribute.
