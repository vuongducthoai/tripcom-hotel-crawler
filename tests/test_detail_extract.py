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

    def test_extracts_album_categories_and_links_room_images(self) -> None:
        album_payload = {
            "data": {
                "hotelImagePop": {
                    "hotelProvide": {
                        "imgTabs": [
                            {
                                "categoryName": "Nổi bật",
                                "categoryId": -1,
                                "imgUrlList": [
                                    {
                                        "subImgUrlList": [
                                            {
                                                "link": "https://ak-d.tripcdn.com/images/room1_R_960_660_R5_D.jpg",
                                                "baseRoomId": 10,
                                                "imgTitle": "Phòng Deluxe",
                                                "diffPositionUrls": [],
                                            }
                                        ]
                                    }
                                ],
                            },
                            {
                                "categoryName": "Phòng",
                                "categoryId": 9,
                                "imgUrlList": [
                                    {
                                        "typeName": "Deluxe Room",
                                        "aggregationName": "Deluxe Room",
                                        "subImgUrlList": [
                                            {
                                                "link": "https://ak-d.tripcdn.com/images/room1_R_960_660_R5_D.jpg",
                                                "baseRoomId": 10,
                                                "imgTitle": "Phòng Deluxe",
                                                "diffPositionUrls": [],
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "categoryName": "Ngoại thất",
                                "categoryId": 1,
                                "imgUrlList": [
                                    {
                                        "subImgUrlList": [
                                            {
                                                "link": "https://ak-d.tripcdn.com/images/exterior1_R_960_660_R5_D.jpg",
                                                "baseRoomId": 0,
                                                "imgTitle": "Toàn cảnh",
                                                "diffPositionUrls": [],
                                            }
                                        ]
                                    }
                                ],
                            },
                        ]
                    },
                    "userProvide": {
                        "imgTabs": [
                            {
                                "categoryName": "Ăn uống",
                                "categoryId": 2,
                                "imgUrlList": [
                                    {
                                        "subImgUrlList": [
                                            {
                                                "link": "https://ak-d.tripcdn.com/images/food1_R_960_660_R5_D.jpg",
                                                "baseRoomId": 0,
                                                "imgTitle": "Bữa sáng",
                                                "diffPositionUrls": [],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    },
                }
            }
        }
        room_payload = {
            "data": {
                "physicRoomMap": {
                    "10": {
                        "name": "Deluxe Room",
                        "bedInfo": {"title": "1 queen bed"},
                        "areaInfo": {"title": "32 m2"},
                    }
                },
                "saleRoomMap": {
                    "sale-1": {
                        "id": 90,
                        "physicalRoomId": 10,
                        "priceInfo": {"price": 1000000, "currency": "VND"},
                    }
                },
            }
        }
        packets = [
            {"url": "https://vn.trip.com/restapi/soa2/28820/ctgethotelalbum", "response": album_payload},
            {"url": "https://vn.trip.com/restapi/soa2/33269/getHotelRoomListOversea", "response": room_payload},
        ]
        result = extract_detail(packets, "105826856", "https://vn.trip.com/hotels/detail/?hotelId=105826856")

        images = result["images"]
        self.assertEqual(3, len(images))

        img_by_cat = {img["category"]: img for img in images}
        self.assertIn("Phòng", img_by_cat)
        self.assertIn("Ngoại thất", img_by_cat)
        self.assertIn("Ăn uống", img_by_cat)

        # Upgraded from Nổi bật to Phòng
        room_img = img_by_cat["Phòng"]
        self.assertEqual("hotel", room_img["source"])
        self.assertEqual("10", room_img["room_id"])
        self.assertNotIn("sub_category", room_img)

        food_img = img_by_cat["Ăn uống"]
        self.assertEqual("user", food_img["source"])

        # Room-level image attachment
        self.assertEqual(1, len(result["rooms"]))
        room0 = result["rooms"][0]
        self.assertIn("images", room0)
        self.assertEqual(1, len(room0["images"]))
        self.assertEqual(room_img["url"], room0["images"][0]["url"])


if __name__ == "__main__":
    unittest.main()

