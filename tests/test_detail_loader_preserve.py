import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from db import detail_loader


class Cursor:
    rowcount = 0
    def __init__(self):
        self.queries = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, params=None): self.queries.append(sql)
    def fetchall(self): return [(123,)]
    def fetchone(self):
        sql = self.queries[-1]
        if 'SELECT id, location_id' in sql: return (123, 1, None)
        return (123,)


class Connection:
    def __init__(self): self.cur = Cursor()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self.cur
    def rollback(self): pass


class PreserveTests(unittest.TestCase):
    def test_preserve_skips_all_hotel_amenity_mutations_with_replace(self):
        payload = {'locale': 'vi-VN', 'currency': 'VND', 'details': [{
            'trip_hotel_id': '1', 'success': True, 'name': 'Test hotel',
            'amenities': [{'name': 'Wrong stale amenity', 'code': '999'}],
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            conn = Connection()
            args = argparse.Namespace(file=str(path),locale=None,currency=None,
                no_prices=True,replace_existing=True,dry_run=True,preserve_hotel_amenities=True)
            with patch.object(detail_loader.psycopg2, 'connect', return_value=conn), contextlib.redirect_stdout(io.StringIO()):
                detail_loader.main(args)
        writes = [q.lower() for q in conn.cur.queries if q.strip().lower().startswith(('insert', 'update', 'delete'))]
        self.assertFalse(any('hotel_amenities' in q or 'hotel_amenity_translations' in q for q in writes))
        self.assertTrue(any('insert into hotel_translations' in q for q in writes))
        self.assertTrue(any('delete from room_amenity_translations' in q for q in writes))


if __name__ == '__main__': unittest.main()
