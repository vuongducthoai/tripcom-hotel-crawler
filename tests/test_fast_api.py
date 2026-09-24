"""Kiểm tra phần thay số liệu khi gọi lại API — offline, không cần mạng."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fast_api as fa  # noqa: E402

MAU = {
    "hotel_id": "744865",
    "check_in": "2026-10-05", "check_out": "2026-10-06",
    "context": {"cityId": 260, "provinceId": 10482},
    "apis": {
        "getHotelRoomListOversea": {
            "url": "https://vn.trip.com/restapi/soa2/33269/getHotelRoomListOversea",
            "method": "POST", "headers": {"content-type": "application/json"},
            "post_data": '{"search":{"hotelId":744865,"checkIn":"20261005",'
                         '"checkOut":"20261006","roomQuantity":1},'
                         '"head":{"locale":"vi-VN"}}',
        },
        "ctGetNearbyPlaceInfo": {
            "url": "https://vn.trip.com/restapi/soa2/28820/ctGetNearbyPlaceInfo",
            "method": "POST", "headers": {},
            "post_data": '{"cityId":0,"masterHotelId":744865,"provinceId":10482}',
        },
    },
}
CTX = {"cityId": 301, "provinceId": 99}


class ThaySoLieu(unittest.TestCase):
    def test_thay_ma_khach_san_va_ngay(self):
        _, _, body = fa.payload_for(MAU, "getHotelRoomListOversea", "123456",
                                    "2026-11-20", "2026-11-21", CTX)
        self.assertEqual(body["search"]["hotelId"], 123456)
        self.assertEqual(body["search"]["checkIn"], "20261120")
        self.assertEqual(body["search"]["checkOut"], "20261121")

    def test_giu_nguyen_truong_khong_lien_quan(self):
        _, _, body = fa.payload_for(MAU, "getHotelRoomListOversea", "123456",
                                    "2026-11-20", "2026-11-21", CTX)
        self.assertEqual(body["search"]["roomQuantity"], 1)
        self.assertEqual(body["head"]["locale"], "vi-VN")

    def test_thay_ma_tinh_thanh(self):
        _, _, body = fa.payload_for(MAU, "ctGetNearbyPlaceInfo", "123456",
                                    "2026-11-20", "2026-11-21", CTX)
        self.assertEqual(body["masterHotelId"], 123456)
        self.assertEqual(body["provinceId"], 99)
        self.assertEqual(body["cityId"], 0, "cityId=0 của mẫu không phải mã thành phố, giữ nguyên")

    def test_khong_doi_gi_khi_cung_khach_san(self):
        _, _, body = fa.payload_for(MAU, "ctGetNearbyPlaceInfo", "744865",
                                    "2026-10-05", "2026-10-06",
                                    {"cityId": 260, "provinceId": 10482})
        self.assertEqual(body["masterHotelId"], 744865)
        self.assertEqual(body["provinceId"], 10482)

    def test_header_va_url_duoc_giu(self):
        url, headers, _ = fa.payload_for(MAU, "getHotelRoomListOversea", "1",
                                         "2026-11-20", "2026-11-21", CTX)
        self.assertTrue(url.endswith("getHotelRoomListOversea"))
        self.assertEqual(headers["content-type"], "application/json")

    def test_dong_bo_visitor_id_voi_cookie_hien_tai(self):
        mau = copy.deepcopy(MAU)
        body = json.loads(mau["apis"]["getHotelRoomListOversea"]["post_data"])
        body["head"].update(cid="old", vid="old")
        mau["apis"]["getHotelRoomListOversea"]["post_data"] = json.dumps(body)
        _, _, result = fa.payload_for(
            mau, "getHotelRoomListOversea", "123456",
            "2026-10-05", "2026-10-06", CTX,
            visitor_id="current-ubt-vid")
        self.assertEqual(result["head"]["cid"], "current-ubt-vid")
        self.assertEqual(result["head"]["vid"], "current-ubt-vid")

    def test_context_tu_khoi_detail(self):
        ctx = fa.context_from_detail({"hotelBaseInfo": {"cityId": 7, "provinceId": 8}})
        self.assertEqual(ctx, {"cityId": 7, "provinceId": 8})
        self.assertEqual(fa.context_from_detail(None), {"cityId": None, "provinceId": None})


if __name__ == "__main__":
    unittest.main()
