"""Repair property amenities only; preserve detail raw, rooms, prices and progress.

Each hotel is captured and committed separately. Re-running skips completed
checkpoints. Old amenity rows/translations are backed up before replacement.
Default: 2 workers with separate pages and DB connections; failed source jobs
are retried after the first pass. Dry runs write amenity_previews, not real checkpoints.
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time
from contextlib import closing, ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import config
import psycopg2
from psycopg2.extras import Json, RealDictCursor
from playwright.async_api import (async_playwright, Error as BrowserError,
                                  TimeoutError as BrowserTimeoutError)
from db.i18n import language_key
from detail_extract import PARSER_VERSION, extract_detail
from hotel_facilities import wait_for_structured_facilities, MissingFacilitiesSource

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def write_json(path, value, attempts=6):
    """Runtime artifacts, not source-file edits. Atomic checkpoint replacement.

    Trên Windows, antivirus/indexer/cloud-sync có thể giữ file vừa ghi trong
    chốc lát khiến os.replace báo WinError 5. Thử lại vài lần rồi mới chịu thua.
    Tên file tạm gắn PID để 2 tiến trình chạy song song không giẫm chân nhau.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.stem}.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    for attempt in range(attempts):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.2 * (attempt + 1))


def write_artifact(path, value, label=''):
    """Checkpoint/log không phải dữ liệu gốc: ghi hỏng thì làm lại lượt sau, không dừng run.

    KHÔNG dùng cho file backup trước khi ghi đè DB — chỗ đó hỏng thì phải dừng thật.
    """
    try:
        write_json(path, value)
        return True
    except OSError as exc:
        print(f'{label} WARNING: không ghi được {path.name} ({exc}); '
              f'khách sạn này sẽ làm lại ở lượt chạy sau', flush=True)
        return False


def completed_checkpoint(path):
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
        return ((cached.get('imported') or cached.get('status') == 'empty_source')
                and cached.get('parser_version') == PARSER_VERSION)
    except (OSError, ValueError):
        return False


class HotelNotFound(Exception):
    """The source explicitly returned its hotel 404 page; do not touch DB."""


def validate_navigation(status, final_url, hotel_id):
    parsed = urlparse(final_url)
    trusted_host = parsed.hostname in {'vn.trip.com', 'www.trip.com', 'trip.com'}
    if trusted_host and (status == 404 or
            (status == 200 and parsed.path.rstrip('/') == '/hotels/pages/404')):
        raise HotelNotFound('Hotel page not found (404); DB unchanged')
    if status is not None and status >= 400:
        raise RuntimeError(f'HTTP {status}; DB unchanged')
    actual_id = parse_qs(parsed.query).get('hotelId', [None])[0]
    if not trusted_host or parsed.path.rstrip('/') != '/hotels/detail' or actual_id != str(hotel_id):
        raise RuntimeError('Redirected to another hotel or verification page; DB unchanged')


async def run_jobs(jobs, workers, handler):
    """A job belongs to exactly one worker; cancel peers if a worker fails."""
    queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    async def worker(number):
        while not queue.empty():
            job = queue.get_nowait()
            try:
                await handler(number, job)
            finally:
                queue.task_done()
    async with asyncio.TaskGroup() as group:
        for number in range(workers):
            group.create_task(worker(number))


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
        if args.limit and len(result) >= args.limit:
            break
        raw = config.OUTPUT_DIR / 'details' / 'raw' / locale / ('VND' if locale == 'vi-VN' else 'USD') / (row['trip_hotel_id'] + '.json')
        legacy = config.OUTPUT_DIR / 'details' / 'raw' / (row['trip_hotel_id'] + '.json')
        if not raw.exists() and locale == 'vi-VN':
            raw = legacy
        if not raw.exists():
            continue
        checkpoint = config.OUTPUT_DIR / 'amenity_repairs' / locale / (row['trip_hotel_id'] + '.json')
        if not args.force and completed_checkpoint(checkpoint):
            result.append(dict(row))
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
    root = config.OUTPUT_DIR / ('amenity_repairs' if args.apply else 'amenity_previews')
    with closing(psycopg2.connect(config.dsn(), connect_timeout=10)) as conn:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel=args.browser_channel)
            try:
                for locale in args.locale:
                    print(f'{locale}: scanning existing successful raw...', flush=True)
                    hotels = targets(conn, args, locale)
                    conn.commit()
                    jobs = [(i, hotel) for i, hotel in enumerate(hotels, 1)
                            if args.force or not completed_checkpoint(root / locale / (hotel['trip_hotel_id'] + '.json'))]
                    print(f'{locale}: {len(hotels)} total; {len(hotels)-len(jobs)} checkpoints skipped; '
                          f'{len(jobs)} pending; workers={args.workers}', flush=True)
                    context = await browser.new_context(locale=locale, viewport=config.VIEWPORT)
                    async def route_assets(route):
                        if route.request.resource_type in {'image', 'media', 'font'}:
                            await route.abort()
                        else:
                            await route.continue_()
                    await context.route('**/*', route_assets)
                    pages = [await context.new_page() for _ in range(args.workers)]
                    retry_jobs = []
                    errors = [0] * args.workers
                    summary = {'success': 0, 'empty': 0, 'not_found': 0, 'unresolved': 0}
                    with ExitStack() as stack:
                        connections = [stack.enter_context(closing(psycopg2.connect(config.dsn(), connect_timeout=10)))
                                       for _ in range(args.workers)]
                        async def handle(worker, job, retry=False):
                            index, hotel = job
                            page, worker_conn = pages[worker], connections[worker]
                            path = root / locale / (hotel['trip_hotel_id'] + '.json')
                            label = f'[{index}/{len(hotels)} W{worker+1}] {hotel["trip_hotel_id"]}'
                            domain = 'vn' if locale == 'vi-VN' else 'www'
                            currency = 'VND' if locale == 'vi-VN' else 'USD'
                            url = f'https://{domain}.trip.com/hotels/detail/?hotelId={hotel["trip_hotel_id"]}&curr={currency}'
                            try:
                                    response = await page.goto(url, wait_until='domcontentloaded', timeout=config.PAGE_TIMEOUT_MS)
                                    validate_navigation(response.status if response else None,
                                                        page.url, hotel['trip_hotel_id'])
                                    source = await wait_for_structured_facilities(
                                        page, args.source_timeout_ms, hotel['trip_hotel_id'])
                                    artifact = {'hotel': hotel, 'locale': locale, 'source': source,
                                        'parser_version': PARSER_VERSION, 'captured_at': datetime.now(timezone.utc).isoformat(), 'imported': False}
                                    if source.get('status') == 'empty_source':
                                        artifact['status'] = 'empty_source'
                                        write_artifact(path, artifact, label)
                                        summary['empty'] += 1
                                        print(f'{label} SKIP empty source; DB unchanged')
                                    else:
                                        detail = extract_detail([{'url': 'embedded:hotel-facilities', 'response': source}],
                                                                hotel['trip_hotel_id'], url, currency, locale)
                                        artifact['detail'] = detail
                                        write_artifact(path, artifact, label)
                                        if args.apply:
                                            backup = root / 'backups' / locale / (hotel['trip_hotel_id'] + '.json')
                                            replace_amenities(worker_conn, hotel, locale, detail, backup)
                                            artifact['imported'] = True
                                            # DB đã commit xong; checkpoint hỏng chỉ khiến
                                            # khách sạn này được làm lại ở lượt sau.
                                            write_artifact(path, artifact, label)
                                        summary['success'] += 1
                                        print(f'{label} OK {len(detail["amenities"])} amenities; imported={artifact["imported"]}')
                                    errors[worker] = 0
                            except HotelNotFound as exc:
                                worker_conn.rollback()
                                errors[worker] = 0
                                summary['not_found'] += 1
                                write_artifact(root / 'errors' / locale / (hotel['trip_hotel_id'] + '.json'),
                                    {'hotel': hotel, 'status': 'not_found', 'error': str(exc),
                                     'url': page.url, 'captured_at': datetime.now(timezone.utc).isoformat()})
                                # No success checkpoint and no immediate retry: try next run.
                                print(f'{label} SKIP 404; DB unchanged; retry next run')
                            except (TimeoutError, BrowserTimeoutError) as exc:
                                worker_conn.rollback()
                                deferred = isinstance(exc, MissingFacilitiesSource)
                                errors[worker] = 0 if deferred else errors[worker] + 1
                                write_artifact(root / 'errors' / locale / (hotel['trip_hotel_id'] + '.json'),
                                    {'hotel': hotel, 'status': 'deferred_source' if deferred else 'timeout',
                                     'error': str(exc), 'url': page.url, 'captured_at': datetime.now(timezone.utc).isoformat()})
                                retry_jobs.append(job)
                                print(f'{label} {"DEFER" if deferred else "TIMEOUT"}; queued for {"next run" if retry else "end-of-pass retry"}; DB unchanged')
                                if errors[worker] >= 5:
                                    raise RuntimeError('Stopped after 5 consecutive timeouts on a worker; checkpoints preserved') from exc
                            except BrowserError as exc:
                                # Lỗi tạm của trình duyệt (trang tự điều hướng, frame bị
                                # tháo, renderer trục trặc): chỉ bỏ qua khách sạn này,
                                # các worker khác vẫn chạy tiếp. Chỉ dừng khi lỗi liên tục.
                                worker_conn.rollback()
                                errors[worker] += 1
                                write_artifact(root / 'errors' / locale / (hotel['trip_hotel_id'] + '.json'),
                                    {'hotel': hotel, 'status': 'browser_error', 'error': str(exc),
                                     'url': page.url, 'captured_at': datetime.now(timezone.utc).isoformat()})
                                retry_jobs.append(job)
                                print(f'{label} BROWSER ERROR; queued for {"next run" if retry else "end-of-pass retry"}; DB unchanged')
                                if errors[worker] >= 5:
                                    raise RuntimeError('Stopped after 5 consecutive browser errors on a worker; checkpoints preserved') from exc
                            except Exception as exc:
                                worker_conn.rollback()
                                write_artifact(root / 'errors' / locale / (hotel['trip_hotel_id'] + '.json'),
                                    {'hotel': hotel, 'status': 'fatal', 'error': str(exc), 'url': page.url,
                                     'captured_at': datetime.now(timezone.utc).isoformat()})
                                # HTTP blocks, driver failures and DB errors stop peers immediately.
                                raise RuntimeError(f'{label} stopped safely: {exc}') from exc
                            await asyncio.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))

                        await run_jobs(jobs, args.workers, handle)
                        for attempt in range(args.retries):
                            pending, retry_jobs = retry_jobs, []
                            if not pending:
                                break
                            print(f'{locale}: retry pass {attempt+1}/{args.retries}, {len(pending)} hotels')
                            await run_jobs(pending, args.workers, lambda worker, job: handle(worker, job, retry=True))
                        summary['unresolved'] = len(retry_jobs)
                        print(f'{locale}: SUMMARY {summary}; unresolved hotels will retry next run')
                    try:
                        await context.close()
                    except Exception as exc:
                        print(f'Context cleanup warning: {exc}')
            finally:
                try:
                    await browser.close()
                except Exception as exc:
                    print(f'Browser cleanup warning (original failure preserved): {exc}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--city-id', type=int, default=301)
    parser.add_argument('--hotel-id')
    parser.add_argument('--locale', nargs='+', choices=['vi-VN', 'en-US'], default=['vi-VN', 'en-US'])
    parser.add_argument('--limit', type=int)
    parser.add_argument('--browser-channel', default=None)
    parser.add_argument('--apply', action='store_true', help='Replace only hotel amenities in DB; backup first')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--retries', type=int, choices=range(0, 4), default=1, help='Number of end-of-pass retry rounds')
    parser.add_argument('--workers', type=int, choices=[1, 2, 3], default=2, help='Concurrent pages; each worker owns a DB connection')
    parser.add_argument('--source-timeout-ms', type=int, default=20000)
    asyncio.run(main(parser.parse_args()))
