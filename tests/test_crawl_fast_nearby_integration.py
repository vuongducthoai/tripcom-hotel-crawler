import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crawl_fast import crawl_one_fast


class TestCrawlFastNearbyIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_crawl_one_fast_uses_ajax_when_successful(self):
        client = AsyncMock()
        client.get_hotel_page.return_value = (
            '<html><script>self.__next_f.push([1,"{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":111,\\"hotelName\\":\\"Test\\",\\"cityId\\":1,\\"provinceId\\":1},\\"hotelPositionInfo\\":{\\"lat\\":21.0,\\"lng\\":105.0,\\"placeInfo\\":{\\"wholePoiInfoList\\":[{\\"poiId\\":1,\\"poiName\\":\\"SSR 1\\"}]}}}}"])</script></html>',
            None,
        )
        client.get_nearby_places.return_value = (
            {
                "data": {
                    "placeInfoList": [
                        {"id": 2, "name": "Giao thông", "places": [{"id": 101, "name": "Ga 1"}, {"id": 102, "name": "Ga 2"}]},
                        {"id": 3, "name": "Điểm nổi bật", "places": [{"id": 201, "name": "Điểm 1"}]},
                    ]
                }
            },
            None,
        )

        target = {"trip_hotel_id": "111", "hotel_name": "Test", "url": "https://vn.trip.com/hotels/detail/?hotelId=111"}
        normalized = await crawl_one_fast(
            client=client,
            target=target,
            checkin="2026-10-05",
            checkout="2026-10-06",
            locale="vi-VN",
            currency="VND",
            no_nearby_ajax=False,
        )

        self.assertEqual(normalized.get("nearby_source"), "ajax")
        self.assertGreaterEqual(len(normalized.get("nearby_places") or []), 3)
        client.get_nearby_places.assert_awaited_once()

    async def test_crawl_one_fast_falls_back_to_ssr_on_ajax_failure(self):
        client = AsyncMock()
        client.get_hotel_page.return_value = (
            '<html><script>self.__next_f.push([1,"{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":111,\\"hotelName\\":\\"Test\\",\\"cityId\\":1,\\"provinceId\\":1},\\"hotelPositionInfo\\":{\\"lat\\":21.0,\\"lng\\":105.0,\\"placeInfo\\":{\\"wholePoiInfoList\\":[{\\"poiId\\":1,\\"poiName\\":\\"SSR 1\\"}]}}}}"])</script></html>',
            None,
        )
        client.get_nearby_places.return_value = (None, "HTTP 430 whaleguard block")

        target = {"trip_hotel_id": "111", "hotel_name": "Test", "url": "https://vn.trip.com/hotels/detail/?hotelId=111"}
        normalized = await crawl_one_fast(
            client=client,
            target=target,
            checkin="2026-10-05",
            checkout="2026-10-06",
            locale="vi-VN",
            currency="VND",
            no_nearby_ajax=False,
        )

        self.assertEqual(normalized.get("nearby_source"), "ssr_fallback")
        self.assertEqual(len(normalized.get("nearby_places") or []), 1)
        client.get_nearby_places.assert_awaited_once()

    async def test_crawl_one_fast_skips_ajax_when_disabled(self):
        client = AsyncMock()
        client.get_hotel_page.return_value = (
            '<html><script>self.__next_f.push([1,"{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":111,\\"hotelName\\":\\"Test\\",\\"cityId\\":1,\\"provinceId\\":1},\\"hotelPositionInfo\\":{\\"lat\\":21.0,\\"lng\\":105.0,\\"placeInfo\\":{\\"wholePoiInfoList\\":[{\\"poiId\\":1,\\"poiName\\":\\"SSR 1\\"}]}}}}"])</script></html>',
            None,
        )

        target = {"trip_hotel_id": "111", "hotel_name": "Test", "url": "https://vn.trip.com/hotels/detail/?hotelId=111"}
        normalized = await crawl_one_fast(
            client=client,
            target=target,
            checkin="2026-10-05",
            checkout="2026-10-06",
            locale="vi-VN",
            currency="VND",
            no_nearby_ajax=True,
        )

        self.assertEqual(normalized.get("nearby_source"), "ssr_fallback")
        self.assertEqual(len(normalized.get("nearby_places") or []), 1)
        client.get_nearby_places.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
