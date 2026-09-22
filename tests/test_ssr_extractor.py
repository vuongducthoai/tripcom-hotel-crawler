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


if __name__ == "__main__":
    unittest.main()
