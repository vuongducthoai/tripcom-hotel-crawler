import sys
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine.http_client_v2 import FastHttpClient


class TestHttpClientNearby(unittest.IsolatedAsyncioTestCase):
    async def test_get_nearby_places_success(self):
        client = FastHttpClient()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "ResponseStatus": {"Ack": "Success"},
            "data": {
                "placeInfoList": [
                    {"id": 2, "name": "Giao thông", "places": [{"id": 101, "name": "Ga Cát Linh"}]},
                    {"id": 3, "name": "Điểm nổi bật", "places": [{"id": 201, "name": "Hồ Gươm"}]},
                ]
            }
        }

        with patch("engine.http_client_v2.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.post.return_value = fake_response
            mock_session_cls.return_value.__aenter__.return_value = mock_session

            data, err = await client.get_nearby_places(
                hotel_id="104981087",
                city_id=260,
                province_id=10482,
                lat=55.676,
                lng=12.568,
                locale="vi-VN",
                currency="VND",
                referer="https://vn.trip.com/hotels/detail/?hotelId=104981087",
            )
            self.assertIsNone(err)
            self.assertIsNotNone(data)
            self.assertEqual(len(data["data"]["placeInfoList"]), 2)

    async def test_get_nearby_places_failure(self):
        client = FastHttpClient()
        fake_response = MagicMock()
        fake_response.status_code = 430
        fake_response.text = "whaleguard block"

        with patch("engine.http_client_v2.AsyncSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.post.return_value = fake_response
            mock_session_cls.return_value.__aenter__.return_value = mock_session

            data, err = await client.get_nearby_places(
                hotel_id="104981087",
                city_id=260,
                province_id=10482,
                lat=55.676,
                lng=12.568,
                locale="vi-VN",
                currency="VND",
                referer="https://vn.trip.com/hotels/detail/?hotelId=104981087",
            )
            self.assertIsNone(data)
            self.assertIn("430", err)


if __name__ == "__main__":
    unittest.main()
