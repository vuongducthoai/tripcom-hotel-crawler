from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from detail_extract import PARSER_VERSION, extract_detail


class DetailExtractTests(unittest.TestCase):
    def test_extracts_hotel_policies_from_modal_and_faq_fallback(self) -> None:
        payloads = [
            {
                "url": "embedded:json-ld",
                "response": {
                    "@type": "FAQPage",
                    "mainEntity": [{
                        "name": "What is the cancellation policy?",
                        "acceptedAnswer": {"text": "It depends on the selected room."},
                    }],
                },
            },
            {
                "url": "embedded:hotel-policies",
                "response": {"text": (
                    "Check-in and check-out times\nCheck-in: After 14:00\n"
                    "Check-out: Before 12:00\nChild policies\nChildren of all ages are welcome\n"
                    "Pets\nPets are not allowed"
                )},
            },
        ]
        result = extract_detail(payloads, "123", "https://example.test/hotel", "USD", "en-US")
        policies = {item["code"]: item for item in result["policies"]}
        self.assertEqual(
            {"checkin_checkout", "children", "pets", "cancellation"}, set(policies)
        )
        self.assertIn("After 14:00", policies["checkin_checkout"]["description"])
        self.assertEqual("Pets are not allowed", policies["pets"]["description"])

    def test_derives_room_and_other_categories_when_source_has_no_label(self) -> None:
        room_url = "https://ak-d.tripcdn.com/images/room.jpg"
        other_url = "https://ak-d.tripcdn.com/images/general.jpg"
        payload = {
            "data": {
                "physicRoomMap": {
                    "10": {
                        "pictureInfo": [{"url": room_url}],
                        "physicalFacilityList": [
                            {"id": 107, "title": "Air conditioning"}
                        ],
                    }
                },
                "otherImages": [{"url": other_url}],
                "serviceList": [{"id": 999, "name": "Mystery service"}],
            }
        }
        result = extract_detail(
            [{"url": "test", "response": payload}],
            "123", "https://example.test/hotel", "USD", "en-US",
        )
        image_categories = {
            image["url"]: {category["name"] for category in image["categories"]}
            for image in result["images"]
        }
        self.assertEqual({"Rooms"}, image_categories[room_url])
        self.assertEqual({"Other"}, image_categories[other_url])
        amenity_categories = {
            item["name"]: item["category"] for item in result["amenities"]
        }
        self.assertEqual("Room amenities", amenity_categories["Air conditioning"])
        self.assertEqual("Other", amenity_categories["Mystery service"])

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
                        "areaInfo": {"title": "301 ft²"},
                        "windowInfo": {"title": "City view"},
                        "smokeInfo": {"title": "Non-smoking"},
                        "wifiInfo": {"title": "Free Wi-Fi"},
                        "floorInfo": {"title": "Floor: 2-7"},
                        "faciltityInfo": {
                            "list": [{
                                "id": 20,
                                "title": "Cleaning services",
                                "subList": [{
                                    "id": 606,
                                    "title": "Daily housekeeping",
                                    "freeType": 0,
                                }],
                            }],
                        },
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
                "roomPopInfo": {
                    "10": {
                        "id": 10,
                        "name": "Deluxe Room",
                        "roomBasicInfo": {
                            "bedInfo": {
                                "addBed": {"title": "Extra beds are available"},
                            },
                            "physicalFacilityList": [
                                {"id": 92, "title": "Private bathroom"},
                            ],
                        },
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
        self.assertEqual(28.0, result["rooms"][0]["area_sqm"])
        self.assertEqual("City view", result["rooms"][0]["view_name"])
        self.assertEqual("Non-smoking", result["rooms"][0]["smoking_policy"])
        self.assertEqual("Free Wi-Fi", result["rooms"][0]["wifi"])
        self.assertEqual("Floor: 2-7", result["rooms"][0]["floor_label"])
        self.assertEqual("Extra beds are available", result["rooms"][0]["extra_bed_policy"])
        self.assertEqual(1, len(result["rooms"][0]["images"]))
        self.assertEqual(
            {"Daily housekeeping", "Private bathroom"},
            {item["name"] for item in result["rooms"][0]["amenities"]},
        )
        self.assertEqual(1, len(result["images"]))


if __name__ == "__main__":
    unittest.main()
