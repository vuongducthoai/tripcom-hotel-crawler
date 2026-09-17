"""Repair property amenities only; preserve detail raw, rooms, prices and progress.

Each hotel is captured and committed separately. Re-running skips completed
checkpoints. Old amenity rows/translations are backed up before replacement.
"""
import argparse
import asyncio
import json
import random
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import config
import psycopg2
from psycopg2.extras import Json, RealDictCursor
from playwright.async_api import async_playwright
from db.i18n import language_key
from detail_extract import PARSER_VERSION, extract_detail
from hotel_facilities import capture_hotel_facilities

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def write_json(path, value):
    # Runtime artifacts, not source-file edits. Atomic checkpoint replacement.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def targets(conn, args, locale):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''SELECT h.id, h.trip_hotel_id FROM hotels h
            JOIN locations l ON l.id=h.location_id
            WHERE l.trip_location_id=%s
              AND (%s IS NULL OR h.trip_hotel_id=%s)
            ORDER BY h.id''', (f'city:{args.city_id}', args.hotel_id, args.hotel_id))
        rows = list(cur.fetchall())
    result = []
    for row in rows:
        raw = config.OUTPUT_DIR / 'details' / 'raw' / locale / ('VND' if locale == 'vi-VN' else 'USD') / (row['trip_hotel_id'] + '.json')
        legacy = config.OUTPUT_DIR / 'details' / 'raw' / (row['trip_hotel_id'] + '.json')
        if not raw.exists() and locale == 'vi-VN':
            raw = legacy
        if not raw.exists():
            continue
        try:
            if not json.loads(raw.read_text(encoding='utf-8')).get('normalized', {}).get('success'):
                continue
        except (ValueError, OSError):
            continue
        result.append(dict(row))
    return result[:args.limit] if args.limit else result


def replace_amenities(conn, hotel, locale, detail, backup):
    if not detail.get('hotel_amenities_captured') or not detail.get('amenities'):
        raise ValueError('Refusing replacement without a captured, nonempty property facilities source')
    language = language_key(locale)
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SET LOCAL lock_timeout TO 5000')
            # Serialize repairs for this hotel and keep other-language rows intact.
            cur.execute('SELECT id FROM hotels WHERE id=%s FOR UPDATE', (hotel['id'],))
            cur.execute('SELECT * FROM hotel_amenities WHERE hotel_id=%s', (hotel['id'],))
            old_rows = list(cur.fetchall())
            cur.execute('''SELECT t.* FROM hotel_amenity_translations t
                JOIN hotel_amenities a ON a.id=t.hotel_amenity_id WHERE a.hotel_id=%s''', (hotel['id'],))
            old_translations = list(cur.fetchall())
            if not backup.exists():
                write_json(backup, {'hotel': hotel, 'locale': locale,
                    'amenities': old_rows, 'translations': old_translations})
            cur.execute('''DELETE FROM hotel_amenity_translations t USING hotel_amenities a
                WHERE t.hotel_amenity_id=a.id AND a.hotel_id=%s AND t.locale=%s''', (hotel['id'], language))
            for item in detail['amenities']:
                cur.execute('''SELECT id FROM hotel_amenities WHERE hotel_id=%s
                    AND ((%s IS NOT NULL AND amenity_code=%s) OR amenity_name=%s)
                    ORDER BY (amenity_code=%s) DESC NULLS LAST, id LIMIT 1''',
                    (hotel['id'], item.get('code'), item.get('code'), item['name'], item.get('code')))
                existing = cur.fetchone()
                if existing:
                    aid = existing['id']
                    cur.execute('''UPDATE hotel_amenities SET
                        amenity_code=COALESCE(%s,amenity_code),
                        category=CASE WHEN %s='vi' THEN %s ELSE COALESCE(category,%s) END,
                        is_available=COALESCE(%s,is_available),
                        is_highlight=COALESCE(%s,is_highlight) WHERE id=%s''',
                        (item.get('code'), language, item.get('category'), item.get('category'),
                         item.get('is_available'), item.get('is_highlight'), aid))
                else:
                    cur.execute('''INSERT INTO hotel_amenities
                        (hotel_id,amenity_code,amenity_name,category,is_available,is_highlight)
                        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id''',
                        (hotel['id'], item.get('code'), item['name'], item.get('category'),
                         item.get('is_available'), item.get('is_highlight')))
                    aid = cur.fetchone()['id']
                cur.execute('''INSERT INTO hotel_amenity_translations
                    (hotel_amenity_id,locale,amenity_name,category,fee_label,additional_info)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(hotel_amenity_id,locale)
                    DO UPDATE SET amenity_name=EXCLUDED.amenity_name,category=EXCLUDED.category,
                    fee_label=EXCLUDED.fee_label,additional_info=EXCLUDED.additional_info,updated_at=now()''',
                    (aid, language, item['name'], item.get('category'), item.get('fee_label'),
                     Json(item.get('additional_info') or [])))
            cur.execute('''DELETE FROM hotel_amenities a WHERE a.hotel_id=%s AND NOT EXISTS
                (SELECT 1 FROM hotel_amenity_translations t WHERE t.hotel_amenity_id=a.id)''', (hotel['id'],))


async def main(args):
    root = config.OUTPUT_DIR / 'amenity_repairs'
    with closing(psycopg2.connect(config.dsn(), connect_timeout=10)) as conn:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel=args.browser_channel)
            try:
                errors = 0
                for locale in args.locale:
                    hotels = targets(conn, args, locale)
                    conn.commit()
                    print(f'{locale}: {len(hotels)} hotels with existing successful raw', flush=True)
                    context = await browser.new_context(locale=locale, viewport=config.VIEWPORT)
                    async def route_assets(route):
                        if route.request.resource_type in {'image', 'media', 'font'}:
                            await route.abort()
                        else:
                            await route.continue_()
                    await context.route('**/*', route_assets)
                    page = await context.new_page()
                    for index, hotel in enumerate(hotels, 1):
                        path = root / locale / (hotel['trip_hotel_id'] + '.json')
                        if path.exists() and not args.force:
                            cached = json.loads(path.read_text(encoding='utf-8'))
                            if cached.get('imported') and cached.get('parser_version') == PARSER_VERSION:
                                print(f'[{index}/{len(hotels)}] {hotel["trip_hotel_id"]} checkpoint OK')
                                continue
                        try:
                            domain = 'vn' if locale == 'vi-VN' else 'www'
                            currency = 'VND' if locale == 'vi-VN' else 'USD'
                            url = f'https://{domain}.trip.com/hotels/detail/?hotelId={hotel["trip_hotel_id"]}&curr={currency}'
                            await page.goto(url, wait_until='domcontentloaded', timeout=config.PAGE_TIMEOUT_MS)
                            await page.locator('[class*="hotelFacilityNew_hotelFacilityNew"]').first.wait_for(state='attached', timeout=20000)
                            source = await capture_hotel_facilities(page)
                            if not source or source.get('source') != 'hotelFacilityPopV2':
                                raise ValueError('Missing structured property facilities; DB unchanged')
                            detail = extract_detail([{'url': 'embedded:hotel-facilities', 'response': source}],
                                                    hotel['trip_hotel_id'], url, currency, locale)
                            artifact = {'hotel': hotel, 'locale': locale, 'source': source, 'detail': detail,
                                'parser_version': PARSER_VERSION, 'captured_at': datetime.now(timezone.utc).isoformat(), 'imported': False}
                            write_json(path, artifact)
                            if args.apply:
                                backup = root / 'backups' / locale / (hotel['trip_hotel_id'] + '.json')
                                replace_amenities(conn, hotel, locale, detail, backup)
                                artifact['imported'] = True
                                write_json(path, artifact)
                            print(f'[{index}/{len(hotels)}] {hotel["trip_hotel_id"]} OK {len(detail["amenities"])} amenities; imported={artifact["imported"]}')
                            errors = 0
                        except Exception as exc:
                            conn.rollback()
                            errors += 1
                            print(f'[{index}/{len(hotels)}] {hotel["trip_hotel_id"]} ERROR {type(exc).__name__}: {exc}')
                            if errors >= 5:
                                raise SystemExit('Stopped after 5 consecutive errors; existing checkpoints preserved')
                        await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
                    await context.close()
            finally:
                await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city-id', type=int, default=301)
    parser.add_argument('--hotel-id')
    parser.add_argument('--locale', nargs='+', choices=['vi-VN', 'en-US'], default=['vi-VN', 'en-US'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--browser-channel', default=None)
    parser.add_argument('--apply', action='store_true', help='Replace only hotel amenities in DB; backup first')
    parser.add_argument('--force', action='store_true')
    asyncio.run(main(parser.parse_args()))
