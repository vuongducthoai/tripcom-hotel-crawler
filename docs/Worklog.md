# Project Worklog

### 2026-09-18

| Member | Task | Status | Output |
| :--- | :--- | :--- | :--- |
| Vương Đức Thoại | Xây dựng module trích xuất tiện nghi tổng quan khách sạn (`src/hotel_facilities.py`), bổ sung migrations 006 đến 011 chuẩn hóa dữ liệu song ngữ/giá/tọa độ/trạng thái tiện nghi, xây dựng giao diện web tra cứu dữ liệu (`src/web_app.py`, `web/`) và các script kiểm toán dữ liệu | ✅ Done | [`src/hotel_facilities.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/hotel_facilities.py), [`src/web_app.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/web_app.py), `migrations/006-011`; Commit [`ceb4415`](https://github.com/vuongducthoai/tripcom-hotel-crawler/commit/ceb441516b431aeb75b325aa95cce6ce1e0869d6) |
| Trần Đăng Nguyên | Đồng bộ hợp nhất `origin/main` vào `CrawlingIssue`, dung hợp trọn vẹn cơ chế bóc tách tiện nghi khách sạn mới với hệ thống worker đa luồng và tối ưu tốc độ, nghiệm thu 13/13 unit tests đạt chuẩn | ✅ Done | [`src/crawl_detail.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/crawl_detail.py), [`tests/test_detail_extract.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/tests/test_detail_extract.py) |

**Tổng kết ngày**: Tích hợp thành công các nâng cấp cốt lõi từ `main`: trích xuất chuẩn xác tiện nghi tổng thể khách sạn từ `window.__next_f` và DOM (kèm trạng thái khả dụng `is_available`), chuẩn hóa hệ thống bảng đa ngôn ngữ ISO `vi`/`en` (migrations 006-011), giao diện web đọc dữ liệu trực quan và bộ công cụ kiểm toán toàn vẹn dữ liệu. Toàn bộ 13/13 unit tests pass hoàn hảo.

---

### 2026-09-17

| Member | Task | Status | Output |
| :--- | :--- | :--- | :--- |
| Vương Đức Thoại | Cập nhật logic thu thập và tái phân tích dữ liệu chi tiết (`src/crawl_detail.py`, `scripts/reparse_details.py`), tối ưu hóa xử lý lỗi và đồng bộ dữ liệu | ✅ Done | [`src/crawl_detail.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/crawl_detail.py), [`scripts/reparse_details.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/scripts/reparse_details.py); Commit [`f273eb6`](https://github.com/vuongducthoai/tripcom-hotel-crawler/commit/f273eb6040d03a08b1c2243f48b3afbd152d4da0) |
| Trần Đăng Nguyên | Đồng bộ và hợp nhất nhánh `CrawlingIssue` với `origin/main`, tích hợp trọn vẹn kiến trúc song ngữ, chính sách khách sạn và bóc tách phòng chi tiết | ✅ Done | Commit [`70f3084`](https://github.com/vuongducthoai/tripcom-hotel-crawler/commit/70f30840cb5c5a50f102482a714464c4298c3216) |
| Trần Đăng Nguyên | Kiểm thử và xác thực toàn diện pipeline bóc tách chi tiết (`test_detail_extract.py`), đảm bảo 6/6 unit tests đạt chuẩn trên codebase mới nhất | ✅ Done | [`tests/test_detail_extract.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/tests/test_detail_extract.py) |
| Trần Đăng Nguyên | Kiểm tra tính nhất quán của dữ liệu thô (`output/details/raw/`) với mô hình nạp cơ sở dữ liệu mới (`room_types`, `room_images`, `hotel_policies`, `nearby_places`) | ✅ Done | [`output/details/raw/`](https://github.com/vuongducthoai/tripcom-hotel-crawler/tree/CrawlingIssue/output/details/raw), [`src/db/detail_loader.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/db/detail_loader.py) |
| Vương Đức Thoại | Nghiên cứu benchmark và tối ưu hóa hiệu năng module crawl detail (`src/crawl_detail.py`): áp dụng chặn tải tài nguyên tĩnh (Resource Aborting), cơ chế chờ gói tin thông minh (Smart Event Wait) và kiến trúc đa luồng an toàn (Concurrency Worker Pool 3 tabs), rút ngắn thời gian thu thập từ ~12s xuống ~4.5s/khách sạn với độ toàn vẹn dữ liệu 100% | ✅ Done | [`src/crawl_detail.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/crawl_detail.py); Commit [`08e0b0c`](https://github.com/vuongducthoai/tripcom-hotel-crawler/commit/08e0b0ccbe2dc27d1a58c142c3ddfefcfc067d02) |

**Tổng kết ngày**: Hoàn tất hợp nhất nhánh làm việc với `origin/main`, đảm bảo codebase sạch, đồng bộ toàn bộ các tính năng bóc tách song ngữ, chính sách khách sạn, tiện ích và phòng chi tiết; tối ưu hóa toàn diện hiệu năng thu thập dữ liệu chi tiết tăng tốc x2.7 lần an toàn; duy trì 6/6 unit tests pass.

---

### 2026-09-16

| Member | Task | Status | Output |
| :--- | :--- | :--- | :--- |
| Vương Đức Thoại | Xây dựng migration cơ sở dữ liệu song ngữ bổ sung bảng dịch thuật (`hotel_translations`, `room_type_translations`, `hotel_amenity_translations`, `hotel_image_categories`) và giá đa tiền tệ | ✅ Done | [`migrations/002_multilingual.sql`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/migrations/002_multilingual.sql) |
| Vương Đức Thoại | Xây dựng các migration mở rộng chi tiết phòng, chính sách khách sạn, phí tiện ích và địa điểm lân cận (`room_details`, `hotel_policies`, `amenity_fees_nearby_places`) | ✅ Done | [`migrations/003_room_details.sql`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/migrations/003_room_details.sql), [`migrations/004_hotel_policies.sql`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/migrations/004_hotel_policies.sql), [`migrations/005_amenity_fees_nearby_places.sql`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/migrations/005_amenity_fees_nearby_places.sql) |
| Vương Đức Thoại | Cập nhật luồng crawl chi tiết hỗ trợ đa ngôn ngữ (`vi-VN`, `en-US`), trích xuất thông tin popup phòng (`roomPopInfo`), chính sách (`embedded:hotel-policies`, FAQ) và địa điểm lân cận | ✅ Done | [`src/crawl_detail.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/crawl_detail.py), [`src/detail_extract.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/detail_extract.py) |
| Vương Đức Thoại | Cập nhật database loader nạp đầy đủ dữ liệu đa ngôn ngữ cho khách sạn, loại phòng, ảnh phòng (`room_images`), tiện ích, chính sách và địa điểm lân cận | ✅ Done | [`src/db/detail_loader.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/db/detail_loader.py), [`src/db/export_sql_sample.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/db/export_sql_sample.py) |
| Vương Đức Thoại | Xuất bản dump cơ sở dữ liệu mẫu 5 khách sạn song ngữ phục vụ kiểm thử | ✅ Done | [`output/data/tripcom_5_hotels_bilingual_dump.sql`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/output/data/tripcom_5_hotels_bilingual_dump.sql); Commit [`e98f634`](https://github.com/vuongducthoai/tripcom-hotel-crawler/commit/e98f6345404ffba4726b8bd7edc37956b1886a4f) |
| Trần Đăng Nguyên | Phân tích cấu trúc gói API chi tiết `ctgethotelalbum`, `getHotelRoomListOversea` và các payload raw để chuẩn hóa bóc tách album ảnh và phòng | ✅ Done | [`src/detail_extract.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/src/detail_extract.py) |
| Trần Đăng Nguyên | Xây dựng và hoàn thiện bộ kiểm thử unit test bóc tách dữ liệu chi tiết (`test_detail_extract.py`) bao gồm kiểm tra bóc tách chính sách, tiện ích phân loại và địa điểm lân cận | ✅ Done | [`tests/test_detail_extract.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/tests/test_detail_extract.py) |
| Trần Đăng Nguyên | Rà soát và kiểm thử quy trình nạp dữ liệu offline qua `scripts/reparse_details.py` trên tập mẫu khách sạn thực tế | ✅ Done | [`scripts/reparse_details.py`](https://github.com/vuongducthoai/tripcom-hotel-crawler/blob/CrawlingIssue/scripts/reparse_details.py) |

**Tổng kết ngày**: Vương Đức Thoại hoàn thành thiết kế và triển khai toàn bộ hệ thống schema cơ sở dữ liệu mở rộng (migrations 002 đến 005) bao gồm song ngữ, chi tiết phòng, chính sách khách sạn và địa điểm lân cận; nâng cấp pipeline crawl và xuất bản dump 5 khách sạn mẫu. Trần Đăng Nguyên phân tích cấu trúc payload raw, phối hợp kiểm thử bóc tách dữ liệu, xây dựng bộ test unit test bao quát các trường thông tin mới và nghiệm thu tính toàn vẹn của dữ liệu nạp DB.
