"""Create a standalone PostgreSQL dump for a small bilingual hotel sample."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import psycopg2
from psycopg2.extras import Json

import config


def sql_value(value: Any) -> Any:
    return Json(value) if isinstance(value, (dict, list)) else value


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def emit_rows(cur, output, table: str, query: str, params: tuple) -> int:
    cur.execute(query, params)
    columns = [desc.name for desc in cur.description]
    rows = cur.fetchall()
    if not rows:
        return 0

    output.write(f"\n-- {table}: {len(rows)} rows\n")
    column_sql = ", ".join(quoted(column) for column in columns)
    placeholder = "(" + ",".join(["%s"] * len(columns)) + ")"
    for row in rows:
        values = tuple(sql_value(value) for value in row)
        rendered = cur.mogrify(placeholder, values).decode("utf-8")
        output.write(
            f"INSERT INTO public.{quoted(table)} ({column_sql}) VALUES {rendered} "
            "ON CONFLICT DO NOTHING;\n"
        )
    return len(rows)


def main(args: argparse.Namespace) -> None:
    json_path = Path(args.json)
    if not json_path.is_absolute():
        json_path = config.ROOT / json_path
    sample = json.loads(json_path.read_text(encoding="utf-8"))
    trip_ids = [str(item["hotel"]["trip_hotel_id"]) for item in sample.get("hotels") or []]
    if not trip_ids:
        raise SystemExit(f"Không có hotel trong {json_path}")

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = config.ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    schema_001 = (config.ROOT / "migrations" / "001_init.sql").read_text(encoding="utf-8")
    schema_002 = (config.ROOT / "migrations" / "002_multilingual.sql").read_text(encoding="utf-8")

    table_queries = [
        (
            "locations",
            """
            WITH RECURSIVE selected_locations AS (
                SELECT DISTINCT l.*
                FROM locations l
                JOIN hotels h ON h.location_id=l.id
                WHERE h.trip_hotel_id=ANY(%s)
                UNION
                SELECT parent.*
                FROM locations parent
                JOIN selected_locations child ON child.parent_id=parent.id
            )
            SELECT * FROM selected_locations ORDER BY id
            """,
        ),
        (
            "location_translations",
            """
            SELECT t.* FROM location_translations t
            WHERE t.location_id IN (
                SELECT DISTINCT location_id FROM hotels
                WHERE trip_hotel_id=ANY(%s) AND location_id IS NOT NULL
            ) ORDER BY t.location_id, t.locale
            """,
        ),
        ("hotels", "SELECT * FROM hotels WHERE trip_hotel_id=ANY(%s) ORDER BY id"),
        (
            "hotel_translations",
            """
            SELECT t.* FROM hotel_translations t JOIN hotels h ON h.id=t.hotel_id
            WHERE h.trip_hotel_id=ANY(%s) ORDER BY t.hotel_id, t.locale
            """,
        ),
        (
            "hotel_images",
            """
            SELECT i.* FROM hotel_images i JOIN hotels h ON h.id=i.hotel_id
            WHERE h.trip_hotel_id=ANY(%s) ORDER BY i.id
            """,
        ),
        (
            "hotel_image_categories",
            """
            SELECT c.* FROM hotel_image_categories c
            JOIN hotel_images i ON i.id=c.hotel_image_id
            JOIN hotels h ON h.id=i.hotel_id
            WHERE h.trip_hotel_id=ANY(%s)
            ORDER BY c.hotel_image_id, c.category_code, c.locale
            """,
        ),
        (
            "hotel_amenities",
            """
            SELECT a.* FROM hotel_amenities a JOIN hotels h ON h.id=a.hotel_id
            WHERE h.trip_hotel_id=ANY(%s) ORDER BY a.id
            """,
        ),
        (
            "hotel_amenity_translations",
            """
            SELECT t.* FROM hotel_amenity_translations t
            JOIN hotel_amenities a ON a.id=t.hotel_amenity_id
            JOIN hotels h ON h.id=a.hotel_id
            WHERE h.trip_hotel_id=ANY(%s)
            ORDER BY t.hotel_amenity_id, t.locale
            """,
        ),
        (
            "room_types",
            """
            SELECT r.* FROM room_types r JOIN hotels h ON h.id=r.hotel_id
            WHERE h.trip_hotel_id=ANY(%s) ORDER BY r.id
            """,
        ),
        (
            "room_type_translations",
            """
            SELECT t.* FROM room_type_translations t
            JOIN room_types r ON r.id=t.room_type_id
            JOIN hotels h ON h.id=r.hotel_id
            WHERE h.trip_hotel_id=ANY(%s)
            ORDER BY t.room_type_id, t.locale
            """,
        ),
        (
            "hotel_prices",
            """
            SELECT p.* FROM hotel_prices p JOIN hotels h ON h.id=p.hotel_id
            WHERE h.trip_hotel_id=ANY(%s) ORDER BY p.id
            """,
        ),
    ]

    counts: dict[str, int] = {}
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        with output_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write("-- Trip.com bilingual sample database dump\n")
            output.write(f"-- Generated: {datetime.now().isoformat(timespec='seconds')}\n")
            output.write(f"-- Hotels: {', '.join(trip_ids)}\n")
            output.write("-- Restore: psql -v ON_ERROR_STOP=1 -d <database> -f <this-file>\n\n")
            output.write(schema_001.rstrip() + "\n\n")
            output.write(schema_002.rstrip() + "\n\n")
            output.write("BEGIN;\nSET client_encoding = 'UTF8';\n")
            for table, query in table_queries:
                counts[table] = emit_rows(cur, output, table, query, (trip_ids,))

            for table in (
                "locations", "hotels", "hotel_images", "hotel_amenities",
                "room_types", "hotel_prices",
            ):
                output.write(
                    f"SELECT setval(pg_get_serial_sequence('public.{table}', 'id'), "
                    f"GREATEST(COALESCE((SELECT max(id) FROM public.{quoted(table)}), 1), 1), true);\n"
                )
            output.write("COMMIT;\n")

    print(f"Đã tạo PostgreSQL dump {len(trip_ids)} hotel → {output_path}")
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--json", default="output/data/mentor_sample_5_hotels_bilingual.json",
        help="file JSON dùng để chọn đúng trip_hotel_id",
    )
    parser.add_argument(
        "--output", default="output/data/tripcom_5_hotels_bilingual_dump.sql",
        help="file SQL đầu ra",
    )
    main(parser.parse_args())
