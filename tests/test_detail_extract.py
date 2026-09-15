from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detail_extract import PARSER_VERSION, extract_detail


class DetailExtractTests(unittest.TestCase):
    def test_joins_physical_and_sale_rooms_and_deduplicates_images(self) -> None:
        payload = {
            "data": {
                "physicRoomMap": {
                    "10": {
                        "name": "Deluxe Room",
                        "bedInfo": {"title": "1 queen bed"},
                        "areaInfo": {"title": "32 m2"},
                        "pictureInfo": [
                            {"url": "https://ak-d.tripcdn.com/images/abc_R_960_660_R5_D.jpg"},
                            {"url": "https://ak-d.tripcdn.com/images/abc_R_200_133_R5_D.jpg"},
                        ],
                    }
                },
                "saleRoomMap": {
                    "sale-a": {
                        "id": 90,
                        "physicalRoomId": 10,
                        "guestCountInfo": {"guestCount": 2},
                        "priceInfo": {"price": 900000, "currency": "VND"},
                        "totalPriceInfo": {"payTax": {"price": 70000}},
                    },
                    "sale-b": {
                        "id": 91,
                        "physicalRoomId": 10,
                        "guestCountInfo": {"guestCount": 2},
                        "priceInfo": {"price": 850000, "currency": "VND"},
                        "totalPriceInfo": {"payTax": {"price": 60000}},
                    },
                },
            }
        }

        result = extract_detail(
            [{"url": "getHotelRoomListOversea", "response": payload}],
            "123",
            "https://example.test/hotel",
        )

        self.assertEqual(PARSER_VERSION, result["parser_version"])
        self.assertEqual(1, len(result["rooms"]))
        self.assertEqual(850000, result["rooms"][0]["price"])
        self.assertEqual(2, result["rooms"][0]["max_occupancy"])
        self.assertFalse(result["rooms"][0]["tax_included"])
        self.assertEqual(1, len(result["images"]))


if __name__ == "__main__":
    unittest.main()
