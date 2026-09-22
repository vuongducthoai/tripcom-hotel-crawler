"""Crawl and update only localized hotel descriptions, with resumable checkpoints.

Example:
  python scripts/repair_hotel_descriptions.py --city-id 301 --locale vi-VN --apply --workers 2
"""
import argparse
import asyncio
import json
import random
import sys
from contextlib import closing, ExitStack
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import config
import psycopg2
from psycopg2.extras import RealDictCursor
from playwright.async_api import async_playwright
from db.i18n import language_key
from hotel_description import capture_hotel_description, description_text
from repair_hotel_amenities import validate_navigation, write_artifact, run_jobs

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def is_complete(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value.get('status') in {'imported', 'empty_source'} and value.get('filter_version') == 2
    except (OSError, ValueError):
        return False


def load_targets(conn, city_id, hotel_id=None, limit=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('''SELECT h.id, h.trip_hotel_id
            FROM hotels h JOIN locations l ON l.id=h.location_id
            WHERE l.trip_location_id=%s
              AND (%s IS NULL OR h.trip_hotel_id=%s)
            ORDER BY h.id''', (f'city:{city_id}', hotel_id, hotel_id))
        rows = [dict(row) for row in cur.fetchall()]
    return rows[:limit] if limit else rows


def save_description(conn, hotel, locale, description):
    language = language_key(locale)
    with conn:
        with conn.cursor() as cur:
            cur.execute('SET LOCAL lock_timeout TO 5000')
            cur.execute('SELECT id FROM hotels WHERE id=%s FOR UPDATE', (hotel['id'],))
            cur.execute('''INSERT INTO hotel_translations
                (hotel_id,locale,description,crawled_at,updated_at)
                VALUES (%s,%s,%s,now(),now())
                ON CONFLICT(hotel_id,locale) DO UPDATE SET
                  description=EXCLUDED.description,
                  crawled_at=EXCLUDED.crawled_at,
                  updated_at=now()''', (hotel['id'], language, description))


async def main(args):
    root = config.OUTPUT_DIR / ('description_repairs' if args.apply else 'description_previews') / args.locale
    with closing(psycopg2.connect(config.dsn(), connect_timeout=10)) as conn:
        hotels = load_targets(conn, args.city_id, args.hotel_id, args.limit)
        conn.commit()
    jobs = [(index, hotel) for index, hotel in enumerate(hotels, 1)
            if args.force or not is_complete(root / f'{hotel["trip_hotel_id"]}.json')]
    print(f'{args.locale}: {len(hotels)} total; {len(hotels)-len(jobs)} checkpoints skipped; '
          f'{len(jobs)} pending; workers={args.workers}')

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False, channel=args.browser_channel)
        context = await browser.new_context(locale=args.locale, viewport=config.VIEWPORT)
        async def route_assets(route):
            if route.request.resource_type in {'image', 'media', 'font'}:
                await route.abort()
            else:
                await route.continue_()
        await context.route('**/*', route_assets)
        pages = [await context.new_page() for _ in range(args.workers)]
        summary = {'descriptions_found': 0, 'imported': 0, 'empty_source': 0, 'errors': 0}
        try:
            with ExitStack() as stack:
                connections = [stack.enter_context(closing(psycopg2.connect(config.dsn(), connect_timeout=10)))
                               for _ in range(args.workers)]
                async def handle(worker, job):
                    index, hotel = job
                    page, db = pages[worker], connections[worker]
                    hotel_id = hotel['trip_hotel_id']
                    path = root / f'{hotel_id}.json'
                    domain = 'vn' if args.locale == 'vi-VN' else 'www'
                    currency = 'VND' if args.locale == 'vi-VN' else 'USD'
                    url = f'https://{domain}.trip.com/hotels/detail/?hotelId={hotel_id}&curr={currency}'
                    label = f'[{index}/{len(hotels)} W{worker+1}] {hotel_id}'
                    try:
                        response = await page.goto(url, wait_until='domcontentloaded', timeout=config.PAGE_TIMEOUT_MS)
                        validate_navigation(response.status if response else None, page.url, hotel_id)
                        source = await capture_hotel_description(page, hotel_id)
                        description = description_text((source or {}).get('hotelDescriptionInfo'))
                        status = 'empty_source'
                        if description:
                            if args.apply:
                                save_description(db, hotel, args.locale, description)
                            status = 'imported' if args.apply else 'preview'
                            summary['descriptions_found'] += 1
                            if args.apply:
                                summary['imported'] += 1
                            print(f'{label} OK {len(description)} chars; imported={args.apply}')
                        else:
                            summary['empty_source'] += 1
                            print(f'{label} EMPTY; DB unchanged')
                        write_artifact(path, {'hotel': hotel, 'locale': args.locale,
                            'status': status, 'filter_version': 2,
                            'description': description,
                            'captured_at': datetime.now(timezone.utc).isoformat()}, label)
                    except Exception as exc:
                        db.rollback()
                        summary['errors'] += 1
                        write_artifact(root / 'errors' / f'{hotel_id}.json', {
                            'hotel': hotel, 'error': f'{type(exc).__name__}: {exc}',
                            'captured_at': datetime.now(timezone.utc).isoformat()}, label)
                        print(f'{label} ERROR {type(exc).__name__}: {exc}; retry next run')
                    await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
                await run_jobs(jobs, args.workers, handle)
        finally:
            await context.close()
            await browser.close()
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city-id', type=int, required=True)
    parser.add_argument('--locale', choices=('vi-VN', 'en-US'), default='vi-VN')
    parser.add_argument('--hotel-id')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--workers', type=int, choices=(1, 2, 3, 4), default=2)
    parser.add_argument('--browser-channel', choices=('chrome', 'msedge'), default=None)
    parser.add_argument('--apply', action='store_true', help='update only hotel_translations.description')
    parser.add_argument('--force', action='store_true', help='ignore successful checkpoints')
    asyncio.run(main(parser.parse_args()))
