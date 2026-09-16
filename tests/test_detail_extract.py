from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detail_extract import PARSER_VERSION, extract_detail


class DetailExtractTests(unittest.TestCase):
    def test_joins_parent_facility_category_to_amenity_items(self) -> None:
        payload = {
            "data": {
                "facilityInfo": {
                    "allFacilities": [
                        {
                            "id": 20,
                            "content": "Cleaning services",
                            "items": [
                                {"id": 606, "content": "Daily housekeeping"},
                                {"id": 607, "content": "Laundry service"},
                            ],
                        }
                    ]
                }
            }
        }
        result = extract_detail(
            [{"url": "room-facilities", "response": payload}],
            "123", "https://example.test/hotel",
        )
        self.assertEqual(2, len(result["amenities"]))
        self.assertEqual(
            {"Cleaning services"},
            {item["category"] for item in result["amenities"]},
        )
        self.assertNotIn(
            "Cleaning services", {item["name"] for item in result["amenities"]}
        )

    def test_does_not_treat_generic_category_as_hotel_type(self) -> None:
        result = extract_detail(
            [{
                "url": "test",
                "response": {
                    "promotion": {"categoryName": "免费早餐"},
                    "album": {"categoryName": "Featured"},
                },
            }],
            "123", "https://example.test/hotel",
        )
        self.assertIsNone(result["hotel_type"])

        result = extract_detail(
            [{"url": "test", "response": {"hotelTypeName": "Boutique hotel"}}],
            "123", "https://example.test/hotel",
        )
        self.assertEqual("Boutique hotel", result["hotel_type"])

    def test_joins_album_tab_categories_to_images(self) -> None:
        url = "https://ak-d.tripcdn.com/images/hotel_R_960_660_R5_D.jpg"
        payload = {
            "data": {
                "hotelImagePop": {
                    "hotelProvide": {
                        "imgTabs": [
                            {
                                "categoryId": -1,
                                "categoryName": "Nổi bật",
                                "imgUrlList": [{"subImgUrlList": [{"link": url}]}],
                            },
                            {
                                "categoryId": 1,
                                "categoryName": "Ngoại thất",
                                "imgUrlList": [{"subImgUrlList": [{
                                    "link": url,
                                    "imgTitle": "Ngoại thất khách sạn",
                                }]}],
                            },
                        ]
                    }
                }
            }
        }

        result = extract_detail(
            [{"url": "ctgethotelalbum", "response": payload}],
            "123", "https://example.test/hotel",
        )

        self.assertEqual(1, len(result["images"]))
        categories = result["images"][0]["categories"]
        self.assertEqual({"hotel:-1", "hotel:1"}, {item["code"] for item in categories})
        self.assertEqual(
            "Ngoại thất khách sạn",
            next(item for item in categories if item["code"] == "hotel:1")["image_title"],
        )

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
