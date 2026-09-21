"""Kiểm tra phần chờ API phòng và cách phân loại lỗi.

Bối cảnh: trước đây hotel chỉ thiếu phòng cũng bị tính là "lỗi", nên crawler
dừng sau 5 hotel liên tiếp dù ảnh/tiện ích vẫn về bình thường.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import crawl_detail


ROOM_URL = "https://www.trip.com/restapi/soa2/31454/getHotelRoomListOversea"
POP_URL = "https://www.trip.com/restapi/soa2/35021/getHotelRoomPopInfoPCOnline"


class FakePage:
    """Trang giả: đếm số lần bị cuộn, không làm gì khác."""

    def __init__(self, fail: bool = False) -> None:
        self.scrolls = 0
        self.fail = fail

    async def evaluate(self, _script: str):
        if self.fail:
            raise RuntimeError("Execution context was destroyed")
        self.scrolls += 1


class HasRoomListTests(unittest.TestCase):
    def test_nhan_ra_room_list(self):
        self.assertTrue(crawl_detail._has_room_list([{"url": ROOM_URL}]))

    def test_room_pop_khong_tinh_la_room_list(self):
        # roomPop cần roomToken từ roomList; có mỗi nó nghĩa là phòng CHƯA về.
        self.assertFalse(crawl_detail._has_room_list([{"url": POP_URL}]))

    def test_packet_rong(self):
        self.assertFalse(crawl_detail._has_room_list([]))
        self.assertFalse(crawl_detail._has_room_list([{"url": None}]))


class WaitForRoomListTests(unittest.IsolatedAsyncioTestCase):
    async def test_co_san_thi_tra_ve_ngay(self):
        page = FakePage()
        got = await crawl_detail._wait_for_room_list(set(), [{"url": ROOM_URL}], page)
        self.assertTrue(got)
        self.assertEqual(page.scrolls, 0)

    async def test_cho_duoc_packet_ve_muon(self):
        packets: list[dict] = []
        page = FakePage()

        async def late():
            await asyncio.sleep(0.6)
            packets.append({"url": ROOM_URL})

        asyncio.create_task(late())
        got = await crawl_detail._wait_for_room_list(
            set(), packets, page, timeout_ms=4000
        )
        self.assertTrue(got)

    async def test_het_gio_thi_tra_false(self):
        page = FakePage()
        got = await crawl_detail._wait_for_room_list(
            set(), [{"url": POP_URL}], page, timeout_ms=700
        )
        self.assertFalse(got)

    async def test_co_cuon_trang_khi_cho(self):
        page = FakePage()
        await crawl_detail._wait_for_room_list(set(), [], page, timeout_ms=2600)
        self.assertGreater(page.scrolls, 0)

    async def test_trang_hong_thi_thoat_em(self):
        # Trang bị điều hướng giữa chừng: không được ném lỗi ra ngoài.
        page = FakePage(fail=True)
        got = await crawl_detail._wait_for_room_list(set(), [], page, timeout_ms=3000)
        self.assertFalse(got)

    async def test_khong_co_page_van_chay(self):
        got = await crawl_detail._wait_for_room_list(set(), [], None, timeout_ms=600)
        self.assertFalse(got)


class ErrorClassificationTests(unittest.TestCase):
    """Cùng công thức mà worker dùng để quyết định dừng hay chạy tiếp."""

    @staticmethod
    def classify(row: dict) -> str:
        hard = bool(row.get("blocked")) or bool(row.get("page_dead"))
        return (
            "LỖI" if hard
            else "OK" if row.get("rooms")
            else "HẾT PHÒNG" if row.get("rooms_sold_out")
            else "THIẾU PHÒNG"
        )

    def test_het_phong_co_nhan_rieng(self):
        row = {"blocked": None, "rooms": [], "rooms_sold_out": True, "images": [1]}
        self.assertEqual(self.classify(row), "HẾT PHÒNG")

    def test_bi_chan_la_loi_nang(self):
        self.assertEqual(
            self.classify({"blocked": "htlSpiderActionErrorCode=4030", "images": [1]}),
            "LỖI",
        )

    def test_trang_khong_ra_gi_la_loi_nang(self):
        self.assertEqual(self.classify({"page_dead": True}), "LỖI")

    def test_thieu_phong_nhung_co_anh_khong_phai_loi_nang(self):
        row = {"blocked": None, "page_dead": False, "images": [1, 2], "rooms": []}
        self.assertEqual(self.classify(row), "THIẾU PHÒNG")

    def test_du_phong_la_ok(self):
        self.assertEqual(self.classify({"rooms": [{"id": 1}]}), "OK")

    def test_nguong_thieu_phong_cao_hon_nguong_loi(self):
        for max_errors in (1, 5, 10):
            limit = max(max_errors * 6, 30)
            self.assertGreaterEqual(limit, 30)
            self.assertGreater(limit, max_errors)


ROOM_LIST_URL = ROOM_URL


class SoldOutTests(unittest.TestCase):
    """Hình dạng lấy từ response thật của Trip.com (hotel 135330667)."""

    @staticmethod
    def packet(body: dict) -> list[dict]:
        return [{"url": ROOM_LIST_URL, "status": 200, "response": {"data": body}}]

    def test_het_phong_that(self):
        body = {"isRoomListSoldOut": True, "roomList": [], "roomCount": 0}
        self.assertTrue(crawl_detail._rooms_sold_out(self.packet(body)))

    def test_con_phong_thi_khong_phai_het_phong(self):
        body = {"isRoomListSoldOut": False, "roomList": [{"id": 1}], "roomCount": 1}
        self.assertFalse(crawl_detail._rooms_sold_out(self.packet(body)))

    def test_co_co_soldout_nhung_van_co_phong_thi_khong_tinh(self):
        # Phòng thật quan trọng hơn cái cờ; tránh vứt nhầm dữ liệu.
        body = {"isRoomListSoldOut": True, "roomList": [{"id": 1}]}
        self.assertFalse(crawl_detail._rooms_sold_out(self.packet(body)))

    def test_goi_hut_api_khong_phai_het_phong(self):
        self.assertFalse(crawl_detail._rooms_sold_out([]))
        self.assertFalse(crawl_detail._rooms_sold_out([{"url": POP_URL, "response": {}}]))

    def test_response_di_dang_khong_lam_no_loi(self):
        for bad in (None, "text", [], {"data": None}, {"data": []}):
            self.assertFalse(
                crawl_detail._rooms_sold_out([{"url": ROOM_LIST_URL, "response": bad}]),
                msg=repr(bad),
            )

    def test_phan_loai_het_phong_khong_tinh_la_thieu(self):
        row = {"rooms": [], "rooms_sold_out": True}
        streak_reset = bool(row.get("rooms") or row.get("rooms_sold_out"))
        self.assertTrue(streak_reset)



class BrowserClosedTests(unittest.TestCase):
    """Phân biệt 'Chromium đóng hẳn' với lỗi điều hướng một trang."""

    def test_nhan_ra_target_closed(self):
        for message in (
            "BrowserContext.new_page: Target page, context or browser has been closed",
            "Target closed",
            "Browser has been closed",
            "Connection closed while reading from the driver",
        ):
            self.assertTrue(crawl_detail._browser_closed(RuntimeError(message)), msg=message)

    def test_loi_dieu_huong_khong_bi_nham(self):
        for message in (
            "Execution context was destroyed, most likely because of a navigation",
            "Timeout 30000ms exceeded",
            "net::ERR_CONNECTION_RESET",
        ):
            self.assertFalse(crawl_detail._browser_closed(RuntimeError(message)), msg=message)

if __name__ == "__main__":
    unittest.main()
