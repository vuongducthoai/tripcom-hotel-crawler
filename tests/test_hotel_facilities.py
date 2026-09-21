import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import hotel_facilities
from hotel_facilities import find_facility_payload, normalize_facility_payload, is_confirmed_empty, wait_for_structured_facilities, MissingFacilitiesSource, payload_from_scripts
from detail_extract import extract_detail


class HotelFacilitiesTests(unittest.TestCase):
    def test_script_literal_fallback_checks_hotel_identity(self):
        payload = {'hotelFacility': [], 'ubtData': {'totalFacilityCount': 0}}
        response = {'hotelDetailResponse': {'hotelBaseInfo': {'masterHotelId': 123},
                                          'hotelFacilityPopV2': payload}}
        script = 'self.__next_f.push(' + json.dumps([1, 'Jc:' + json.dumps({'data': json.dumps(response)}) + '\n']) + ');'
        self.assertEqual(payload, payload_from_scripts([script], '123'))
        self.assertIsNone(payload_from_scripts([script], '456'))
        self.assertIsNone(payload_from_scripts(['self.__next_f.push(runCode()); // hotelFacilityPopV2'], '123'))

    def test_unverified_identity_is_rejected(self):
        self.assertIsNone(find_facility_payload({'hotelFacilityPopV2': {'hotelFacility': []}},
                                                expected_hotel_id='123'))
    def test_only_explicit_zero_count_is_confirmed_empty(self):
        self.assertTrue(is_confirmed_empty({'hotelFacility': [], 'ubtData': {'totalFacilityCount': 0}}))
        self.assertFalse(is_confirmed_empty({'hotelFacility': []}))
        self.assertFalse(is_confirmed_empty({'hotelFacility': [], 'ubtData': {'totalFacilityCount': 2}}))
        self.assertFalse(is_confirmed_empty(None))
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


class FacilitiesWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_source_returns_skip_not_success(self):
        page = AsyncMock()
        page.evaluate.return_value = {'hotelFacilityPopV2': {
            'hotelFacility': [], 'ubtData': {'totalFacilityCount': 0}}}
        result = await wait_for_structured_facilities(page)
        self.assertEqual('empty_source', result['status'])
        self.assertFalse(result['captured'])

    async def test_waits_for_late_json(self):
        page = AsyncMock()
        payload = {'hotelFacility': [{'title': 'Internet', 'categoryList': [
            {'list': [{'facilityDesc': 'Wi-Fi', 'code': 102}]}]}]}
        page.evaluate.side_effect = [[], {'hotelFacilityPopV2': payload}]
        with patch('hotel_facilities.asyncio.sleep', new_callable=AsyncMock), patch(
                'hotel_facilities.capture_hotel_facilities', new_callable=AsyncMock) as capture:
            capture.return_value = normalize_facility_payload(payload)
            result = await wait_for_structured_facilities(page)
        self.assertEqual('hotelFacilityPopV2', result['source'])
        self.assertEqual(2, page.evaluate.await_count)

    async def test_missing_source_remains_error(self):
        page = AsyncMock()
        page.evaluate.return_value = []
        with patch('hotel_facilities.capture_hotel_facilities', new_callable=AsyncMock) as capture:
            capture.return_value = None
            with self.assertRaises(TimeoutError) as caught:
                await wait_for_structured_facilities(page, timeout_ms=0)
            self.assertNotIsInstance(caught.exception, MissingFacilitiesSource)

    async def test_visible_facilities_without_json_are_deferred(self):
        page = AsyncMock()
        page.evaluate.return_value = []
        with patch('hotel_facilities.capture_hotel_facilities', new_callable=AsyncMock) as capture:
            capture.return_value = {'source': 'hotel-facilities-dom', 'items': [{'name': 'Gym'}]}
            with self.assertRaises(MissingFacilitiesSource):
                await wait_for_structured_facilities(page, timeout_ms=0)


class NavigationRetryTests(unittest.IsolatedAsyncioTestCase):
    """Trang tự điều hướng là lỗi tạm của 1 khách sạn, không được dừng cả run."""

    PAYLOAD = {'hotelFacility': [{'title': 'Internet', 'categoryList': [
        {'list': [{'facilityDesc': 'Wi-Fi', 'code': 102}]}]}]}
    NAVIGATION = 'Page.evaluate: Execution context was destroyed, most likely because of a navigation'

    async def test_navigation_error_retries_instead_of_killing_run(self):
        page = AsyncMock()
        page.url = 'https://vn.trip.com/hotels/detail/?hotelId=123'
        page.evaluate.side_effect = [
            hotel_facilities.BrowserError(self.NAVIGATION),
            {'hotelBaseInfo': {'masterHotelId': 123}, 'hotelFacilityPopV2': self.PAYLOAD}]
        with patch('hotel_facilities.asyncio.sleep', new_callable=AsyncMock), patch(
                'hotel_facilities.capture_hotel_facilities', new_callable=AsyncMock) as capture:
            capture.return_value = normalize_facility_payload(self.PAYLOAD)
            result = await wait_for_structured_facilities(page, 20000, '123')
        self.assertEqual('hotelFacilityPopV2', result['source'])
        self.assertEqual(2, page.evaluate.await_count)

    async def test_navigating_to_another_hotel_is_refused(self):
        page = AsyncMock()
        page.url = 'https://vn.trip.com/hotels/detail/?hotelId=999'
        page.evaluate.side_effect = hotel_facilities.BrowserError(self.NAVIGATION)
        with patch('hotel_facilities.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(RuntimeError) as caught:
                await wait_for_structured_facilities(page, 20000, '123')
        self.assertIn('navigated away', str(caught.exception))

    async def test_non_navigation_browser_error_still_propagates(self):
        page = AsyncMock()
        page.url = 'https://vn.trip.com/hotels/detail/?hotelId=123'
        page.evaluate.side_effect = hotel_facilities.BrowserError(
            'Target page, context or browser has been closed')
        with patch('hotel_facilities.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaises(hotel_facilities.BrowserError):
                await wait_for_structured_facilities(page, 20000, '123')


if __name__ == '__main__':
    unittest.main()
