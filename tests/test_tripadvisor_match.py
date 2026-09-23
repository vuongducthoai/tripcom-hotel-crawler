"""Kiểm tra phần ghép Tripadvisor bằng API giả — không cần mạng, không cần key.

    python -m unittest tests.test_tripadvisor_match -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tripadvisor_match as tm  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(url)
        for suffix, (status, payload) in self.routes.items():
            if url.endswith(suffix):
                return FakeResponse(status, payload)
        return FakeResponse(404, {})


HOTEL = {"hotel_id": 1, "trip_hotel_id": 2150756, "name": "Copenhagen Marriott Hotel",
         "latitude": 55.6690, "longitude": 12.5790}


def client(routes, max_calls=100):
    return tm.Client("KEY", max_calls, 0, http=FakeHttp(routes))


class Matching(unittest.TestCase):
    def test_names(self):
        self.assertGreaterEqual(tm.name_similarity("Scandic Norreport", "Scandic Nørreport"), 0.95)
        self.assertGreaterEqual(tm.name_similarity("Copenhagen Marriott Hotel", "Copenhagen Marriott"), 0.9)
        self.assertLess(tm.name_similarity("Hotel Mayfair", "Hotel Hans"), 0.55)

    def test_clean_match(self):
        c = client({"/location/search": (200, {"data": [
                        {"location_id": "111", "name": "Some Other Hotel"},
                        {"location_id": "189531", "name": "Copenhagen Marriott Hotel"}]}),
                    "/location/189531/details": (200, {
                        "name": "Copenhagen Marriott Hotel", "rating": "4.5", "num_reviews": "3,812",
                        "web_url": "https://www.tripadvisor.com/Hotel_Review-g189541-d189531-Reviews.html",
                        "latitude": "55.6692", "longitude": "12.5794"})})
        row = tm.match_hotel(c, HOTEL)
        self.assertEqual(row["match_status"], "matched")
        self.assertEqual((row["tripadvisor_location_id"], row["rating"], row["review_count"]), (189531, 4.5, 3812))
        self.assertLess(row["distance_m"], 100)
        self.assertEqual(c.calls, 2)

    def test_far_away_same_name_is_review_not_match(self):
        c = client({"/location/search": (200, {"data": [{"location_id": "5", "name": "Copenhagen Marriott Hotel"}]}),
                    "/location/5/details": (200, {"name": "Copenhagen Marriott Hotel", "rating": "4.0",
                                                  "num_reviews": "10", "latitude": "55.675",
                                                  "longitude": "12.590",
                                                  "web_url": "https://www.tripadvisor.com/x"})})
        self.assertEqual(tm.match_hotel(c, HOTEL)["match_status"], "review")

    def test_different_name_skips_details_call(self):
        c = client({"/location/search": (200, {"data": [{"location_id": "9", "name": "Wakeup Borgergade"}]})})
        row = tm.match_hotel(c, HOTEL)
        self.assertEqual(row["match_status"], "no_match")
        self.assertIsNone(row["tripadvisor_location_id"])
        self.assertEqual(c.calls, 1)

    def test_bad_values_are_dropped(self):
        parsed = tm.parse_details({"rating": "7.5", "num_reviews": "-3", "web_url": "https://evil.example/x"})
        self.assertEqual((parsed["rating"], parsed["review_count"], parsed["tripadvisor_url"]), (None, None, None))

    def test_quota_and_bad_key_stop_the_run(self):
        with self.assertRaises(tm.StopRun):
            tm.match_hotel(client({"/location/search": (429, {})}), HOTEL)
        with self.assertRaises(tm.StopRun):
            tm.match_hotel(client({"/location/search": (401, {})}), HOTEL)
        with self.assertRaises(tm.StopRun):
            tm.match_hotel(client({"/location/search": (200, {"data": []})}, max_calls=0), HOTEL)


if __name__ == "__main__":
    unittest.main()
