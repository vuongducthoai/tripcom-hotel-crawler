import json
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine.content_validator import (
    ANTIBOT_XOR_KEY,
    decode_obfuscated,
    detect_blocked_reason,
    validate_hotel_html,
)


class TestContentValidator(unittest.TestCase):
    def test_decode_obfuscated(self):
        # Create an obfuscated string with XOR 0x0A
        msg = json.dumps({"failedcause": "Antibot-Gray-ip", "htlSpiderActionErrorCode": 4030})
        obfuscated = [ord(c) ^ ANTIBOT_XOR_KEY for c in msg]
        # Pad to at least 50 bytes
        while len(obfuscated) < 60:
            obfuscated.append(ord(" ") ^ ANTIBOT_XOR_KEY)

        decoded = decode_obfuscated(obfuscated)
        self.assertIsNotNone(decoded)
        self.assertIn("failedcause", decoded)

        # Detect blocked reason
        reason = detect_blocked_reason(obfuscated)
        self.assertEqual(reason, "Antibot-Gray-ip")

    def test_detect_dict_block_error_code(self):
        data = {"status": 200, "data": {"htlSpiderActionErrorCode": 4030}}
        reason = detect_blocked_reason(data)
        self.assertEqual(reason, "htlSpiderActionErrorCode=4030")

    def test_validate_empty_or_short_html(self):
        is_valid, reason = validate_hotel_html("")
        self.assertFalse(is_valid)
        self.assertIn("empty", reason.lower())

        is_valid, reason = validate_hotel_html("<html><body>Short</body></html>")
        self.assertFalse(is_valid)
        self.assertIn("short", reason.lower())

    def test_validate_challenge_page(self):
        challenge_html = "<html><head><title>Security</title></head><body>Verify you are human before accessing Trip.com</body></html>" + " " * 500
        is_valid, reason = validate_hotel_html(challenge_html)
        self.assertFalse(is_valid)
        self.assertIn("Anti-bot challenge", reason)

    def test_validate_valid_hotel_html(self):
        valid_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>InterContinental Hanoi - Trip.com</title>
            <meta name="description" content="Khách sạn 5 sao sang trọng tại Hà Nội" />
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Hotel",
                "name": "InterContinental Hanoi Landmark72",
                "address": "Pham Hung, Me Tri, Nam Tu Liem, Hanoi"
            }
            </script>
        </head>
        <body>
            <div id="__next"></div>
            <script>
            (self.__next_f = self.__next_f || []).push([1, "1:HL[{\\"hotelDescriptionInfo\\": {\\"description\\": \\"Good hotel\\"}}]\\n"]);
            </script>
        </body>
        </html>
        """ + (" " * 400)

        is_valid, reason = validate_hotel_html(valid_html, expected_hotel_id=None)
        self.assertTrue(is_valid)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
