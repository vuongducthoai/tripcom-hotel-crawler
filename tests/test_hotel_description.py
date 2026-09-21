import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hotel_description import clean_description, description_from_scripts, description_text
from detail_extract import extract_detail


class HotelDescriptionTests(unittest.TestCase):
    def test_sections_are_joined_without_duplicate_summary(self):
        info = {'description': 'Summary', 'sectionList': [
            {'desc': '<p>First &amp; second.</p>'}, {'desc': 'Another paragraph.'},
            {'desc': '<p>First &amp; second.</p>'},
        ]}
        self.assertEqual(description_text(info), 'First & second.\n\nAnother paragraph.')

    def test_rejects_null_sentinels_and_platform_marketing(self):
        for value in ('null', ' NULL ', 'None', 'undefined', 'N/A', '-'):
            self.assertIsNone(clean_description(value))
        marketing = ('Đại lý du lịch trực tuyến hàng đầu thế giới với các chuyến bay '
                     'tới hơn 5.000 thành phố và 1,2 triệu khách sạn.')
        self.assertIsNone(clean_description(marketing))
        self.assertIsNone(description_text({'sectionList': [
            {'desc': 'null'}, {'desc': marketing},
        ]}))

    def test_fallback_metadata_uses_same_filter(self):
        for value in (
            'null',
            'Đại lý du lịch trực tuyến hàng đầu thế giới với các chuyến bay tới hơn 5.000 thành phố.',
        ):
            result = extract_detail(
                [{'url': 'embedded:page-meta', 'response': {'description': value}}],
                '123', 'https://example.test', 'VND', 'vi-VN',
            )
            self.assertIsNone(result['description'])

    def test_decodes_flight_and_checks_property_identity(self):
        data = {'hotelBaseInfo': {'masterHotelId': 123},
                'hotelDescriptionInfo': {'description': 'The property introduction.'}}
        script = 'self.__next_f.push(' + json.dumps([1, 'a:' + json.dumps(data)]) + ');'
        self.assertEqual(description_from_scripts([script], '123'), data['hotelDescriptionInfo'])
        self.assertIsNone(description_from_scripts([script], '456'))

    def test_prefers_full_property_description_over_seo_and_room_text(self):
        packets = [
            {'url': 'embedded:hotel-description', 'response': {'hotel_id': '123',
             'hotelDescriptionInfo': {'sectionList': [{'desc': 'Real hotel introduction.'},
                                                     {'desc': 'A pool and restaurant.'}]}}},
            {'url': 'getRoomDetail', 'response': {'description': 'Room text ' * 50}},
            {'url': 'api', 'response': {'hotelPolicyInfo': {'description': 'Policy text ' * 50},
                                       'rating': {'description': 'Review text ' * 50}}},
        ]
        result = extract_detail(packets, '123', 'https://example.test', 'USD', 'en-US')
        self.assertEqual(result['description'], 'Real hotel introduction.\n\nA pool and restaurant.')
        self.assertIsNone(extract_detail(packets, '456', 'https://example.test')['description'])

    def test_policy_and_room_only_have_no_hotel_description(self):
        packets = [{'url': 'getRoomDetail', 'response': {'description': 'Room text ' * 50}},
                   {'url': 'api', 'response': {'hotelPolicyInfo': {'description': 'Policy ' * 50}}}]
        self.assertIsNone(extract_detail(packets, '123', 'https://example.test')['description'])


if __name__ == '__main__':
    unittest.main()
