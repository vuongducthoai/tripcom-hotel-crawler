"""Collect property facilities from their rendered section, never room dialogs."""
import json
import asyncio
import re
from urllib.parse import urlparse, parse_qs

try:  # Playwright là driver thật; test chạy với mock nên không bắt buộc.
    from playwright.async_api import Error as BrowserError
except ImportError:  # pragma: no cover
    class BrowserError(Exception):
        """Chỗ giữ tên để module vẫn import được khi thiếu driver."""


class MissingFacilitiesSource(TimeoutError):
    """A real facilities section exists, but its authoritative JSON is absent."""


# Trang detail của Trip.com tự điều hướng/dựng lại khi đang đọc. Đây là lỗi
# tạm thời của một khách sạn, KHÔNG phải site chặn — không được dừng cả run.
NAVIGATION_ERROR_HINTS = (
    'execution context was destroyed',
    'execution context is not available',
    'frame was detached',
    'frame got detached',
    'navigating and changing the content',
)


def is_navigation_error(exc):
    """True khi page.evaluate hỏng chỉ vì trang vừa điều hướng."""
    message = str(exc).lower()
    return any(hint in message for hint in NAVIGATION_ERROR_HINTS)


def on_expected_hotel(page, expected_hotel_id):
    """Sau khi điều hướng, xác nhận trang vẫn là đúng khách sạn đang xử lý.

    Thiếu bước này thì poll lại có thể lấy nhầm payload của khách sạn khác
    rồi ghi vào DB dưới tên khách sạn cũ.
    """
    if expected_hotel_id is None:
        return True
    url = page.url if isinstance(page.url, str) else ''
    parsed = urlparse(url)
    if parsed.hostname not in {'vn.trip.com', 'www.trip.com', 'trip.com'}:
        return False
    if parsed.path.rstrip('/') != '/hotels/detail':
        return False
    return parse_qs(parsed.query).get('hotelId', [None])[0] == str(expected_hotel_id)


def find_facility_payload(value, depth=0, expected_hotel_id=None):
    """Decode JSON data only; never execute page scripts."""
    if depth > 35:
        return None
    if isinstance(value, str) and 'hotelFacilityPopV2' in value:
        try:
            return find_facility_payload(json.loads(value), depth + 1, expected_hotel_id)
        except (ValueError, TypeError):
            for line in value.splitlines():
                if ':' in line:
                    try:
                        found = find_facility_payload(json.loads(line.split(':', 1)[1]), depth + 1, expected_hotel_id)
                        if found:
                            return found
                    except (ValueError, TypeError):
                        pass
    elif isinstance(value, dict):
        payload = value.get('hotelFacilityPopV2')
        if isinstance(payload, dict) and isinstance(payload.get('hotelFacility'), list):
            identity = (value.get('hotelBaseInfo') or {}).get('masterHotelId')
            if expected_hotel_id is None or str(identity) == str(expected_hotel_id):
                return payload
            return None
        for child in value.values():
            found = find_facility_payload(child, depth + 1, expected_hotel_id)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_facility_payload(child, depth + 1, expected_hotel_id)
            if found:
                return found
    return None


def payload_from_scripts(scripts, expected_hotel_id):
    """Decode literal push arguments only, without eval or script execution."""
    decoder = json.JSONDecoder()
    for script in scripts:
        if not isinstance(script, str) or 'hotelFacilityPopV2' not in script:
            continue
        for match in re.finditer(r'(?:self|window)\.__next_f\.push\(\s*', script):
            try:
                argument, _ = decoder.raw_decode(script[match.end():])
            except ValueError:
                continue
            payload = find_facility_payload(argument, expected_hotel_id=expected_hotel_id)
            if payload is not None:
                return payload
    return None


async def read_page_facility_payload(page, expected_hotel_id=None):
    expected = expected_hotel_id
    if expected is None:
        expected = parse_qs(urlparse(page.url).query).get('hotelId', [None])[0] if isinstance(page.url, str) else None
    data = await page.evaluate('''() => ({flight: window.__next_f || [],
        scripts: [...document.scripts].map(s => s.textContent || '')
            .filter(s => s.includes('hotelFacilityPopV2'))})''')
    if isinstance(data, dict) and 'flight' in data:
        # Search scripts first: the runtime array may be consumed or reset.
        payload = payload_from_scripts(data.get('scripts') or [], expected)
        if payload is not None:
            return payload
        return find_facility_payload(data['flight'], expected_hotel_id=expected)
    return find_facility_payload(data, expected_hotel_id=expected)


def normalize_facility_payload(payload):
    items = []
    def add(item, group=None, highlight=False):
        if not isinstance(item, dict) or not item.get('facilityDesc'):
            return
        items.append({
            'name': item['facilityDesc'], 'code': item.get('code'),
            'category': (group or {}).get('title'),
            'category_code': (group or {}).get('categoryId'),
            'fee_label': item.get('showTitle') or None,
            'additional_info': item.get('facilityInfo') or [],
            'is_highlight': highlight,
        })
    for group in payload.get('hotelFacility') or []:
        for category in group.get('categoryList') or []:
            for item in category.get('list') or []:
                add(item, group)
    for item in (payload.get('hotelPopularFacility') or {}).get('list') or []:
        add(item, highlight=True)
    return {'items': items, 'captured': True, 'source': 'hotelFacilityPopV2'} if items else None


def is_confirmed_empty(payload):
    """Only explicit source metadata can establish a valid empty facilities list."""
    return (isinstance(payload, dict)
            and payload.get('hotelFacility') == []
            and not (payload.get('hotelPopularFacility') or {}).get('list')
            and not payload.get('hotelNormalFacilityList')
            and (payload.get('ubtData') or {}).get('totalFacilityCount') == 0)


async def wait_for_structured_facilities(page, timeout_ms=20000, expected_hotel_id=None):
    """Poll streamed page JSON; absence of a DOM section is not an error itself."""
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while True:
        try:
            payload = await read_page_facility_payload(page, expected_hotel_id)
            if is_confirmed_empty(payload):
                return {'status': 'empty_source', 'source': 'hotelFacilityPopV2',
                        'captured': False, 'items': [], 'raw': payload}
            if payload and normalize_facility_payload(payload):
                source = await capture_hotel_facilities(page, payload=payload)
                if source and source.get('source') == 'hotelFacilityPopV2':
                    return source
            if asyncio.get_running_loop().time() >= deadline:
                fallback = await capture_hotel_facilities(page)
                if fallback and fallback.get('source') == 'hotel-facilities-dom':
                    raise MissingFacilitiesSource('Facilities visible in DOM but structured source missing; deferred, DB unchanged')
                raise TimeoutError('Structured property facilities did not arrive; DB unchanged')
        except BrowserError as exc:
            # Trang vừa điều hướng nên context JS bị hủy giữa chừng. Context mới
            # sẽ dựng lại, nên chỉ cần đọc lại — với điều kiện vẫn đúng khách sạn.
            if not is_navigation_error(exc):
                raise
            if not on_expected_hotel(page, expected_hotel_id):
                raise RuntimeError('Page navigated away from the requested hotel; DB unchanged') from exc
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError('Page kept navigating while reading facilities; DB unchanged') from exc
        await asyncio.sleep(0.5)


async def capture_hotel_facilities(page, payload=None):
    availability = await page.evaluate(r'''() => {
        const root = document.querySelector('[class*="hotelFacilityNew_hotelFacilityNew"]');
        if (!root) return {};
        const states = {};
        for (const e of root.querySelectorAll('[class*="popular_desc"]')) {
            const name = (e.querySelector('[class*="popular_detailDesc"]')?.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (!name) continue;
            const unavailable = !!e.querySelector('[class*="underline_unavail"], .ic-defect, .ic_defect')
                || e.className.includes('descUnavail');
            states[name] = states[name] === false ? false : !unavailable;
        }
        return states;
    }''')
    if payload is None:
        payload = await read_page_facility_payload(page)
    if payload:
        structured = normalize_facility_payload(payload)
        if structured:
            for item in structured['items']:
                item['is_available'] = availability.get(' '.join(item['name'].split()).lower())
            return structured
    result = await page.evaluate(r"""() => {
        const root = document.querySelector('[class*="hotelFacilityNew_hotelFacilityNew"]');
        if (!root) return null;
        const text = e => (e?.textContent || '').replace(/\s+/g, ' ').trim();
        const items = [];
        const add = (element, category, highlight) => {
            const name = text(element.querySelector('[class*="popular_detailDesc"]'));
            if (!name) return;
            const fee = text(element.querySelector('[class*="popular_label"]'));
            items.push({name, category, fee_label: fee || null, is_highlight: highlight});
        };
        for (const group of root.querySelectorAll('[class*="hotelFacility-normalA__"]')) {
            const category = text(group.querySelector('[class*="normal_outsideTitle"]'));
            for (const item of group.querySelectorAll('[class*="normal_drawerItem"]'))
                add(item, category || null, false);
        }
        for (const item of root.querySelectorAll('[class*="hotelFacility-popular_item__"]'))
            add(item, null, true);
        // A missing/changed selector is not an authoritative empty facilities list.
        return items.length ? {items, captured: true, source: 'hotel-facilities-dom'} : null;
    }""")
    if result:
        for item in result['items']:
            item['is_available'] = availability.get(' '.join(item['name'].split()).lower())
    return result
