"""Read the property's introduction, not room, policy or marketing text."""
import html
import json
import re


def description_text(info):
    if not isinstance(info, dict):
        return None
    sections = info.get('sectionList') or []
    values = [section.get('desc') for section in sections if isinstance(section, dict)]
    if not any(isinstance(value, str) and value.strip() for value in values):
        values = [info.get('description')]
    paragraphs = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = ' '.join(html.unescape(re.sub(r'<[^>]+>', ' ', value)).split())
        if text and text not in paragraphs:
            paragraphs.append(text)
    return '\n\n'.join(paragraphs) or None


def find_description_info(value, hotel_id, depth=0):
    if depth > 35:
        return None
    if isinstance(value, str) and 'hotelDescriptionInfo' in value:
        try:
            return find_description_info(json.loads(value), hotel_id, depth + 1)
        except ValueError:
            for line in value.splitlines():
                if ':' not in line:
                    continue
                try:
                    found = find_description_info(json.loads(line.split(':', 1)[1]), hotel_id, depth + 1)
                    if found is not None:
                        return found
                except ValueError:
                    pass
    elif isinstance(value, dict):
        info = value.get('hotelDescriptionInfo')
        if isinstance(info, dict):
            identity = (value.get('hotelBaseInfo') or {}).get('masterHotelId')
            if str(identity) == str(hotel_id):
                return info
            # Never descend into a property object whose identity is different.
            if identity is not None:
                return None
        for child in value.values():
            found = find_description_info(child, hotel_id, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_description_info(child, hotel_id, depth + 1)
            if found is not None:
                return found
    return None


def description_from_scripts(scripts, hotel_id):
    decoder = json.JSONDecoder()
    for script in scripts:
        if not isinstance(script, str) or 'hotelDescriptionInfo' not in script:
            continue
        for match in re.finditer(r'(?:self|window)\.__next_f\.push\(\s*', script):
            try:
                argument, _ = decoder.raw_decode(script[match.end():])
            except ValueError:
                continue
            info = find_description_info(argument, hotel_id)
            if info is not None:
                return info
    return None


async def capture_hotel_description(page, hotel_id):
    from hotel_facilities import on_expected_hotel
    if not on_expected_hotel(page, hotel_id):
        return None
    scripts = await page.locator('script').all_text_contents()
    info = description_from_scripts(scripts, hotel_id)
    if not on_expected_hotel(page, hotel_id) or not description_text(info):
        return None
    return {'hotel_id': str(hotel_id), 'hotelDescriptionInfo': info}
