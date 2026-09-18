"""Collect property facilities from their rendered section, never room dialogs."""
import json


def find_facility_payload(value, depth=0):
    """Decode JSON data only; never execute page scripts."""
    if depth > 35:
        return None
    if isinstance(value, str) and 'hotelFacilityPopV2' in value:
        try:
            return find_facility_payload(json.loads(value), depth + 1)
        except (ValueError, TypeError):
            for line in value.splitlines():
                if ':' in line:
                    try:
                        found = find_facility_payload(json.loads(line.split(':', 1)[1]), depth + 1)
                        if found:
                            return found
                    except (ValueError, TypeError):
                        pass
    elif isinstance(value, dict):
        payload = value.get('hotelFacilityPopV2')
        if isinstance(payload, dict) and isinstance(payload.get('hotelFacility'), list):
            return payload
        for child in value.values():
            found = find_facility_payload(child, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_facility_payload(child, depth + 1)
            if found:
                return found
    return None


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


async def capture_hotel_facilities(page):
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
    flight = await page.evaluate('() => window.__next_f || []')
    payload = find_facility_payload(flight)
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
