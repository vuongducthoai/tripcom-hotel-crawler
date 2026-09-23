"""Kiểm tra crawler tĩnh — chạy offline, không cần mạng hay cookie.

    python -m unittest tests.test_crawl_fast -v
"""
from __future__ import annotations

import gzip
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import crawl_fast as cf  # noqa: E402


def fake_html(detail: dict, *, json_ld: dict | None = None,
              description: str | None = None) -> str:
    """Dựng HTML giống trang Trip.com: flight data + JSON-LD + thẻ meta.

    Next.js nhét dữ liệu dưới dạng self.__next_f.push([1,"<chuỗi đã escape>"]),
    nên phải json.dumps hai lần: một lần thành JSON, một lần thành chuỗi JS.
    """
    payload = json.dumps({"hotelDetailResponse": detail}, ensure_ascii=False)
    chunk = json.dumps(payload, ensure_ascii=False)      # thành chuỗi có escape
    parts = ["<html><head>"]
    if description:
        parts.append(f'<meta name="description" content="{description}"/>')
    if json_ld:
        parts.append('<script type="application/ld+json">'
                     + json.dumps(json_ld, ensure_ascii=False) + "</script>")
    parts.append("</head><body><script>self.__next_f.push([1," + chunk
                 + "])</script></body></html>")
    return "".join(parts)


DETAIL = {"hotelBaseInfo": {"masterHotelId": 744865, "openYear": "2006",
                            "starInfo": {"level": 4, "type": "star"}},
          "hotelDescriptionInfo": {"lables": ["Khai Trương: 2006", "Số Phòng: 198"]}}


class BocTuHtml(unittest.TestCase):
    def test_lay_duoc_khoi_detail(self):
        block = cf.detail_block(fake_html(DETAIL))
        self.assertIsNotNone(block)
        self.assertEqual(block["hotelBaseInfo"]["masterHotelId"], 744865)

    def test_html_khong_co_flight_data(self):
        self.assertIsNone(cf.detail_block("<html><body>trống</body></html>"))

    def test_json_ld_va_meta(self):
        html = fake_html(DETAIL, json_ld={"@type": "Hotel", "name": "Hotel Mayfair"},
                         description="Khách sạn trung tâm Copenhagen")
        self.assertEqual(cf.json_ld_blocks(html)[0]["name"], "Hotel Mayfair")
        self.assertEqual(cf.meta_description(html), "Khách sạn trung tâm Copenhagen")

    def test_dung_dinh_dang_packet(self):
        html = fake_html(DETAIL, json_ld={"@type": "Hotel"}, description="mô tả")
        urls = [p["url"] for p in cf.packets_from_html(html)]
        self.assertEqual(urls, [cf.DETAIL_BLOCK_URL, "embedded:json-ld", "embedded:page-meta"])


class NhanDienChan(unittest.TestCase):
    def test_trang_dang_nhap(self):
        self.assertIn("đăng nhập",
                      cf.blocked_reason("<html></html>", "https://vn.trip.com/account/signin"))

    def test_ma_4030(self):
        self.assertIsNotNone(
            cf.blocked_reason('{"htlSpiderActionErrorCode":4030}', "https://vn.trip.com/x"))

    def test_trang_binh_thuong(self):
        self.assertIsNone(cf.blocked_reason(fake_html(DETAIL), "https://vn.trip.com/hotels/detail/"))


class GhepVoiRawThat(unittest.TestCase):
    """Bóc từ HTML giả dựng bằng đúng khối detail của raw thật, so với raw gốc."""

    def test_so_khop_raw_that(self):
        folder = ROOT / "output" / "details" / "raw" / "vi-VN" / "VND"
        # thử vài mã đã biết trước, không quét cả thư mục cho nhanh
        sample = next((p for p in (folder / f"{h}.json.gz" for h in
                                   ("744865", "2150756", "36649443", "134013415"))
                       if p.exists()), None)
        dump = json.loads(gzip.decompress(sample.read_bytes()))
        that = next((r["response"] for r in dump["responses"]
                     if r["url"] == cf.DETAIL_BLOCK_URL), None)
        if that is None:
            self.skipTest("raw mẫu không có khối detail")
        boc = cf.detail_block(fake_html(that))
        self.assertEqual(boc["hotelBaseInfo"].get("masterHotelId"),
                         that["hotelBaseInfo"].get("masterHotelId"))
        self.assertEqual(json.dumps(boc, sort_keys=True), json.dumps(that, sort_keys=True))


class DuongDanVaUrl(unittest.TestCase):
    def test_url_dung_thi_truong(self):
        self.assertTrue(cf.detail_url("1", "vi-VN", "VND", "2026-10-05", "2026-10-06")
                        .startswith("https://vn.trip.com/hotels/detail/?hotelId=1"))
        self.assertIn("www.trip.com",
                      cf.detail_url("1", "en-US", "USD", "2026-10-05", "2026-10-06"))

    def test_url_co_ngay_va_tien_te(self):
        url = cf.detail_url("9", "en-US", "usd", "2026-10-05", "2026-10-06")
        self.assertIn("checkIn=2026-10-05", url)
        self.assertIn("curr=USD", url)


class TienNghiTuSSR(unittest.TestCase):
    """hotelFacilityPopV2 trong SSR thay cho popup DOM mà Chromium phải mở."""

    POP = {"hotelFacilityPopV2": {
        "hotelPopularFacility": {"title": "Phổ biến", "categoryId": 1,
                                 "list": [{"facilityDesc": "Phòng gym", "code": 42}]},
        "hotelFacility": [{"title": "Internet", "categoryId": 2, "categoryList": [
            {"list": [{"facilityDesc": "Wi-Fi chung", "code": 102, "showTitle": "Miễn phí"}]}]}],
        "hotelNormalFacilityList": [{"facilityDesc": "Dịch thuật", "code": 739}],
    }}

    def test_gom_du_ba_nguon(self):
        items = cf.facilities_from_detail(self.POP)["items"]
        self.assertEqual([i["code"] for i in items], [42, 102, 739])

    def test_giu_nhom_va_phi(self):
        items = {i["code"]: i for i in cf.facilities_from_detail(self.POP)["items"]}
        self.assertEqual(items[102]["category"], "Internet")
        self.assertEqual(items[102]["category_code"], 2)
        self.assertEqual(items[102]["fee_label"], "Miễn phí")
        self.assertIsNone(items[739]["category"])

    def test_bo_ma_trung(self):
        pop = {"hotelFacilityPopV2": {
            "hotelPopularFacility": {"title": "Phổ biến", "categoryId": 1,
                                     "list": [{"facilityDesc": "Gym", "code": 42}]},
            "hotelNormalFacilityList": [{"facilityDesc": "Gym", "code": 42}]}}
        self.assertEqual(len(cf.facilities_from_detail(pop)["items"]), 1)

    def test_khong_co_khoi_thi_tra_none(self):
        self.assertIsNone(cf.facilities_from_detail({}))
        self.assertIsNone(cf.facilities_from_detail(None))

    def test_packet_co_khoi_tien_nghi(self):
        html = fake_html(self.POP | {"hotelBaseInfo": {"masterHotelId": 1}})
        urls = [p["url"] for p in cf.packets_from_html(html)]
        self.assertIn("embedded:hotel-facilities", urls)


if __name__ == "__main__":
    unittest.main()
