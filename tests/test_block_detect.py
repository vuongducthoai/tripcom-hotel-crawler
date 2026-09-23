"""Nhận diện Trip.com chặn — kể cả khi giấu trong mảng byte XOR 0x0A."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import block_detect as bd  # noqa: E402


def xor_hoa(text: str) -> list[int]:
    return [ord(c) ^ bd.ANTIBOT_XOR_KEY for c in text]


class NhanDienChan(unittest.TestCase):
    def test_ma_spider_trong_dict(self):
        self.assertEqual(bd.blocked_reason({"htlSpiderActionErrorCode": 4030}),
                         "htlSpiderActionErrorCode=4030")

    def test_ma_spider_nam_sau_trong_cay(self):
        self.assertIsNotNone(
            bd.blocked_reason({"data": {"x": [{"htlSpiderActionErrorCode": 4030}]}}))

    def test_mang_byte_xor_gray_ip(self):
        payload = json.dumps({"failedcause": "Antibot-Gray-ip", "pad": "x" * 60})
        self.assertEqual(bd.blocked_reason({"d": xor_hoa(payload)}), "Antibot-Gray-ip")

    def test_response_binh_thuong(self):
        self.assertIsNone(bd.blocked_reason(
            {"hotelBaseInfo": {"masterHotelId": 744865}, "rooms": [1, 2, 3]}))

    def test_mang_so_binh_thuong_khong_bi_nham(self):
        self.assertIsNone(bd.blocked_reason({"gia": list(range(100, 300))}))

    def test_mang_qua_ngan_bo_qua(self):
        self.assertIsNone(bd.decode_obfuscated(xor_hoa('{"failedcause":"x"}')))


if __name__ == "__main__":
    unittest.main()
