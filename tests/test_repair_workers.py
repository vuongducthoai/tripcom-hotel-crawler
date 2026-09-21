import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('repair', Path(__file__).resolve().parents[1] / 'scripts' / 'repair_hotel_amenities.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_jobs_run_once_with_bounded_concurrency(self):
        seen, active, maximum = [], 0, 0
        async def handler(worker, job):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.005)
            seen.append(job)
            active -= 1
        await repair.run_jobs(list(range(12)), 2, handler)
        self.assertEqual(list(range(12)), sorted(seen))
        self.assertEqual(2, maximum)

    async def test_fatal_error_cancels_other_workers(self):
        cancelled = asyncio.Event()
        async def handler(worker, job):
            if job == 0:
                await asyncio.sleep(0.01)
                raise RuntimeError('database failure')
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        with self.assertRaises(ExceptionGroup):
            await repair.run_jobs([0, 1, 2], 2, handler)
        self.assertTrue(cancelled.is_set())

    async def test_retry_pass_processes_only_failed_jobs(self):
        failed, seen = [], []
        async def first(worker, job):
            seen.append(job)
            if job == 2:
                failed.append(job)
        await repair.run_jobs([1, 2, 3], 2, first)
        retry_seen = []
        async def retry(worker, job):
            retry_seen.append(job)
        await repair.run_jobs(failed, 2, retry)
        self.assertEqual([2], retry_seen)


class CheckpointTests(unittest.TestCase):
    def test_navigation_skips_source_404_only(self):
        for status, url in [
            (200, 'https://vn.trip.com/hotels/pages/404?redirect_url=anything'),
            (404, 'https://vn.trip.com/hotels/detail/?hotelId=123'),
        ]:
            with self.assertRaises(repair.HotelNotFound):
                repair.validate_navigation(status, url, '123')
        repair.validate_navigation(200, 'https://vn.trip.com/hotels/detail/?hotelId=123', '123')

    def test_verification_wrong_hotel_and_http_blocks_remain_fatal(self):
        for status, url in [
            (403, 'https://vn.trip.com/hotels/pages/404'),
            (429, 'https://vn.trip.com/hotels/detail/?hotelId=123'),
            (200, 'https://vn.trip.com/verify?hotelId=123'),
            (200, 'https://vn.trip.com/hotels/detail/?hotelId=456'),
            (200, 'https://example.test/hotels/pages/404'),
        ]:
            with self.assertRaises(RuntimeError):
                repair.validate_navigation(status, url, '123')

    def test_preserves_completed_checkpoint_and_retries_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'hotel.json'
            repair.write_json(path, {'imported': True, 'parser_version': repair.PARSER_VERSION})
            self.assertTrue(repair.completed_checkpoint(path))
            repair.write_json(path, {'status': 'empty_source', 'parser_version': repair.PARSER_VERSION})
            self.assertTrue(repair.completed_checkpoint(path))
            repair.write_json(path, {'status': 'deferred_source', 'parser_version': repair.PARSER_VERSION})
            self.assertFalse(repair.completed_checkpoint(path))


if __name__ == '__main__':
    unittest.main()
