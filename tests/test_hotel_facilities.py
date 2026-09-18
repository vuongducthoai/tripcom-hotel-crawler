import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hotel_facilities import find_facility_payload, normalize_facility_payload
from detail_extract import extract_detail


class HotelFacilitiesTests(unittest.TestCase):
    def test_decodes_flight_and_preserves_real_source_ids(self):
        payload = {
            'hotelFacility': [{'title': 'Wellness', 'categoryId': 9, 'categoryList': [
                {'list': [{'facilityDesc': 'Gym', 'code': 42}]}]}],
            'hotelPopularFacility': {'list': [{'facilityDesc': 'Gym', 'code': 42}]},
        }
        flight = [[1, 'Jc:' + json.dumps({'data': json.dumps({'hotelFacilityPopV2': payload})}) + '\n']]
        self.assertEqual(payload, find_facility_payload(flight))
        normalized = normalize_facility_payload(payload)
        result = extract_detail([{'url': 'embedded:hotel-facilities', 'response': normalized}],
                                '1', 'https://example.test', 'USD', 'en-US')
        self.assertEqual(1, len(result['amenities']))
        item = result['amenities'][0]
        self.assertEqual('42', item['code'])
        self.assertEqual('9', item['category_code'])
        self.assertTrue(item['is_highlight'])

    def test_missing_source_code_is_not_invented(self):
        payload = {'hotelFacility': [{'title': 'Languages', 'categoryId': 17,
            'categoryList': [{'list': [{'facilityDesc': 'English'}]}]}]}
        item = normalize_facility_payload(payload)['items'][0]
        self.assertIsNone(item['code'])
        self.assertEqual(17, item['category_code'])
        self.assertIsNone(find_facility_payload('not JSON hotelFacilityPopV2'))

    def test_availability_preserves_false_true_and_unknown(self):
        source = {'captured': True, 'items': [
            {'name': 'Parking', 'code': 656, 'is_available': False},
            {'name': 'Wi-Fi', 'code': 102, 'is_available': True},
            {'name': 'Unknown facility'},
        ]}
        result = extract_detail([{'url': 'embedded:hotel-facilities', 'response': source}],
                                '1', 'https://example.test', 'USD', 'en-US')
        items = {item['name']: item for item in result['amenities']}
        self.assertIs(items['Parking']['is_available'], False)
        self.assertIs(items['Wi-Fi']['is_available'], True)
        self.assertIsNone(items['Unknown facility']['is_available'])


if __name__ == '__main__':
    unittest.main()
