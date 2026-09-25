import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ssr_extractor import (
    extract_hotel_detail_response,
    extract_json_ld,
    extract_meta_description,
    parse_hotel_html,
)


class TestSsrExtractor(unittest.TestCase):
    def setUp(self):
        self.sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Lotte Hotel Hanoi - Trip.com</title>
            <meta name="description" content="Khách sạn 5 sao cao cấp tại Liễu Giai, Ba Đình, Hà Nội." />
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Hotel",
                "name": "Lotte Hotel Hanoi",
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": "54 Lieu Giai",
                    "addressLocality": "Ba Dinh",
                    "addressRegion": "Hanoi"
                },
                "starRating": {"ratingValue": "5"}
            }
            </script>
        </head>
        <body>
            <script>
            (self.__next_f = self.__next_f || []).push([1, "1:I{\\"hotelBaseInfo\\":{\\"masterHotelId\\":12345},\\"hotelDescriptionInfo\\":{\\"sectionList\\":[{\\"desc\\":\\"Khách sạn tuyệt đẹp nhìn ra hồ Tây và toàn cảnh thành phố Hà Nội.\\"}]}}\\n"]);
            </script>
        </body>
        </html>
        """

    def test_extract_json_ld(self):
        nodes = extract_json_ld(self.sample_html)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].get("name"), "Lotte Hotel Hanoi")
        self.assertEqual(nodes[0].get("@type"), "Hotel")

    def test_extract_meta_description(self):
        meta_desc = extract_meta_description(self.sample_html)
        self.assertEqual(meta_desc, "Khách sạn 5 sao cao cấp tại Liễu Giai, Ba Đình, Hà Nội.")

    def test_parse_hotel_html(self):
        result = parse_hotel_html(
            self.sample_html,
            hotel_id="12345",
            url="https://vn.trip.com/hotels/detail/?hotelId=12345",
            currency="VND",
            locale="vi-VN",
        )
        normalized = result["normalized"]
        self.assertEqual(normalized.get("trip_hotel_id"), "12345")
        self.assertEqual(normalized.get("name"), "Lotte Hotel Hanoi")
        self.assertIn("Lieu Giai", normalized.get("address", ""))
        self.assertTrue(len(result["packets"]) >= 2)

    def test_extract_hotel_detail_response(self):
        html_with_detail = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":99999,\\"starInfo\\":{\\"level\\":5,\\"type\\":\\"star\\"}},\\"hotelPositionInfo\\":{\\"lat\\":\\"10.764\\",\\"lng\\":\\"106.696\\"}}}"]);
        </script>
        </body></html>
        """
        detail = extract_hotel_detail_response(html_with_detail)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["hotelBaseInfo"]["masterHotelId"], 99999)
        self.assertEqual(detail["hotelBaseInfo"]["starInfo"]["level"], 5)

        # Also test via parse_hotel_html
        result = parse_hotel_html(
            html_with_detail,
            hotel_id="99999",
            url="https://vn.trip.com/hotels/detail/?hotelId=99999",
        )
        self.assertTrue(result["normalized"].get("has_detail_block"))
        self.assertTrue(any(p["url"] == "embedded:hotel-detail-response" for p in result["packets"]))

    def test_v2_extract_compatibility(self):
        from v2.extract import find_detail
        html_with_detail = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":99999,\\"starInfo\\":{\\"level\\":4,\\"type\\":\\"star\\"}},\\"hotelPositionInfo\\":{\\"lat\\":\\"10.764\\",\\"lng\\":\\"106.696\\"}}}"]);
        </script>
        </body></html>
        """
        result = parse_hotel_html(
            html_with_detail,
            hotel_id="99999",
            url="https://vn.trip.com/hotels/detail/?hotelId=99999",
        )
        dump = {
            "target": {"trip_hotel_id": 99999},
            "url": "https://vn.trip.com/hotels/detail/?hotelId=99999",
            "normalized": result["normalized"],
            "responses": result["packets"],
        }
        detail = find_detail(dump)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["hotelBaseInfo"]["masterHotelId"], 99999)
        self.assertEqual(detail["hotelBaseInfo"]["starInfo"]["level"], 4)

    def test_extract_policies_bridge(self):
        html_with_policies = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":88888},\\"hotelPolicyInfo\\":{\\"checkInAndOut\\":{\\"title\\":\\"Thời gian nhận và trả phòng\\",\\"content\\":[{\\"title\\":\\"Nhận phòng\\",\\"description\\":\\"Sau 14:00\\"},{\\"title\\":\\"Trả phòng\\",\\"description\\":\\"Trước 12:00\\"}]},\\"childPolicy\\":{\\"title\\":\\"Chính sách cho trẻ em\\",\\"content\\":[{\\"title\\":\\"Trẻ em\\",\\"description\\":\\"Chào đón mọi trẻ em\\"}]}}}}"]);
        </script>
        </body></html>
        """
        result = parse_hotel_html(
            html_with_policies,
            hotel_id="88888",
            url="https://vn.trip.com/hotels/detail/?hotelId=88888",
        )
        normalized = result["normalized"]
        policies = normalized.get("policies") or []
        self.assertGreater(len(policies), 0)
        self.assertTrue(any(p.get("code") == "checkin_checkout" for p in policies))
        self.assertTrue(any("trả phòng" in p.get("title", "").lower() or "check-in" in p.get("title", "").lower() for p in policies))

    def test_extract_rooms_v2_bundle_compatibility(self):
        from v2.extract import build_bundle
        html_with_rooms = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":77777,\\"hotelName\\":\\"Grand Hotel\\",\\"nameInfo\\":{\\"name\\":\\"Grand Hotel\\"}},\\"starInfo\\":{\\"level\\":5}},\\"physicRoomMap\\":{\\"555\\":{\\"id\\":555,\\"name\\":\\"Deluxe King Room\\",\\"areaInfo\\":{\\"title\\":\\"35 m²\\"},\\"houseTypeInfo\\":{\\"bedCount\\":1,\\"bedRoomCount\\":1,\\"bathRoomCount\\":1},\\"smokeInfo\\":{\\"type\\":2},\\"wifiInfo\\":{\\"title\\":\\"Free Wi-Fi\\"}}},\\"roomList\\":[{\\"key\\":\\"555\\"}]}"]);
        </script>
        </body></html>
        """
        result = parse_hotel_html(
            html_with_rooms,
            hotel_id="77777",
            url="https://vn.trip.com/hotels/detail/?hotelId=77777",
            currency="USD",
            locale="en-US",
        )
        dump = {
            "target": {"trip_hotel_id": 77777, "name": "Grand Hotel"},
            "url": "https://www.trip.com/hotels/detail/?hotelId=77777",
            "normalized": {**result["normalized"], "check_in": "2026-10-01", "check_out": "2026-10-02"},
            "responses": result["packets"],
        }
        bundle, issues = build_bundle(dump, raw_locale="en-US", currency="USD")
        self.assertIsNotNone(bundle)
        self.assertEqual(len(bundle.rooms), 1)
        self.assertEqual(bundle.rooms[0].trip_room_id, 555)
        self.assertEqual(bundle.rooms[0].smoking, "non_smoking")

    def test_extract_reviews_v2_compatibility(self):
        from decimal import Decimal
        from v2.extract import build_bundle
        html_with_comments = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":66666,\\"hotelName\\":\\"Review Hotel\\",\\"nameInfo\\":{\\"name\\":\\"Review Hotel\\"}},\\"hotelComment\\":{\\"comment\\":{\\"score\\":\\"9.2\\",\\"scoreDescription\\":\\"Tuyệt vời\\",\\"scoreMax\\":\\"10\\",\\"totalComment\\":120,\\"quality\\":[\\"Phòng sạch sẽ\\"],\\"scoreDetail\\":[{\\"showType\\":\\"Cleanliness\\",\\"showScore\\":\\"9.1\\"},{\\"showType\\":\\"Location\\",\\"showScore\\":\\"9.5\\"},{\\"showType\\":\\"Amenities\\",\\"showScore\\":\\"8.8\\"},{\\"showType\\":\\"Service\\",\\"showScore\\":\\"9.4\\"}]}}}}"]);
        </script>
        </body></html>
        """
        result = parse_hotel_html(
            html_with_comments,
            hotel_id="66666",
            url="https://vn.trip.com/hotels/detail/?hotelId=66666",
            currency="VND",
            locale="vi-VN",
        )
        dump = {
            "target": {"trip_hotel_id": 66666, "name": "Review Hotel"},
            "url": "https://vn.trip.com/hotels/detail/?hotelId=66666",
            "normalized": {**result["normalized"], "check_in": "2026-10-01", "check_out": "2026-10-02"},
            "responses": result["packets"],
        }
        bundle, issues = build_bundle(dump, raw_locale="vi-VN", currency="VND")
        self.assertIsNotNone(bundle)
        self.assertIsNotNone(bundle.review)
        self.assertEqual(bundle.review.rating_overall, Decimal("9.2"))
        self.assertEqual(bundle.review.rating_location, Decimal("9.5"))
        self.assertEqual(bundle.review.rating_facility, Decimal("8.8"))
        self.assertEqual(bundle.review.rating_service, Decimal("9.4"))
        self.assertEqual(bundle.review.rating_cleanliness, Decimal("9.1"))
        self.assertEqual(len(bundle.review_tags), 1)
        self.assertEqual(bundle.review_tags[0].name, "Phòng sạch sẽ")

    def test_extract_nearby_places_bridge(self):
        from v2.extract import build_bundle
        html_with_places = """
        <html><body>
        <script>
        self.__next_f.push([1, "{\\"hotelDetailResponse\\":{\\"hotelBaseInfo\\":{\\"masterHotelId\\":55555,\\"hotelName\\":\\"Place Hotel\\",\\"nameInfo\\":{\\"name\\":\\"Place Hotel\\"}},\\"hotelPositionInfo\\":{\\"placeInfo\\":{\\"wholePoiInfoList\\":[{\\"poiId\\":\\"12345\\",\\"poiName\\":\\"Hồ Hoàn Kiếm\\",\\"distance\\":\\"350m\\",\\"distType\\":\\"WALK\\",\\"poiType\\":\\"5\\",\\"descWithType\\":\\"Điểm tham quan: Hồ Hoàn Kiếm\\"}]}}}}"]);
        </script>
        </body></html>
        """
        result = parse_hotel_html(
            html_with_places,
            hotel_id="55555",
            url="https://vn.trip.com/hotels/detail/?hotelId=55555",
            currency="VND",
            locale="vi-VN",
        )
        # Check legacy normalized
        norm = result["normalized"]
        nearby = norm.get("nearby_places") or []
        self.assertEqual(len(nearby), 1)
        self.assertEqual(nearby[0]["name"], "Hồ Hoàn Kiếm")

        # Check Schema V2 bundle
        dump = {
            "target": {"trip_hotel_id": 55555, "name": "Place Hotel"},
            "url": "https://vn.trip.com/hotels/detail/?hotelId=55555",
            "normalized": {**norm, "check_in": "2026-10-01", "check_out": "2026-10-02"},
            "responses": result["packets"],
        }
        bundle, issues = build_bundle(dump, raw_locale="vi-VN", currency="VND")
        self.assertIsNotNone(bundle)
        self.assertEqual(len(bundle.nearby), 1)
        self.assertEqual(bundle.nearby[0].name, "Hồ Hoàn Kiếm")
        self.assertEqual(bundle.nearby[0].trip_poi_id, 12345)
        self.assertEqual(bundle.nearby[0].travel_mode, "walk")


if __name__ == "__main__":
    unittest.main()



