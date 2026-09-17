"""Local read-only PostgreSQL data viewer for the crawler database.

Run from the repository root:
    python src/web_app.py

The server intentionally binds to 127.0.0.1 and every database connection is
read-only.  No web framework is required.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

import config


WEB_DIR = config.ROOT / "web"
SCHEMA_CACHE_TTL = 30
STATS_CACHE_TTL = 60
_schema_cache: tuple[float, dict[str, Any]] | None = None
_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def db_connection():
    conn = psycopg2.connect(config.dsn())
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '15s'")
        cur.execute("SET lock_timeout = '2s'")
    return conn


def load_schema(*, force: bool = False) -> dict[str, Any]:
    global _schema_cache
    now = time.monotonic()
    if not force and _schema_cache and now - _schema_cache[0] < SCHEMA_CACHE_TTL:
        return _schema_cache[1]

    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT c.table_name, c.column_name, c.data_type, c.udt_name,
                   c.is_nullable = 'YES' AS nullable, c.ordinal_position
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema=c.table_schema AND t.table_name=c.table_name
            WHERE c.table_schema='public' AND t.table_type='BASE TABLE'
            ORDER BY c.table_name, c.ordinal_position
            """
        )
        column_rows = cur.fetchall()
        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name=kcu.constraint_name
             AND tc.table_schema=kcu.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='PRIMARY KEY'
            ORDER BY tc.table_name, kcu.ordinal_position
            """
        )
        pk_rows = cur.fetchall()
        cur.execute(
            """
            SELECT tc.constraint_name, kcu.table_name, kcu.column_name,
                   ccu.table_name AS ref_table, ccu.column_name AS ref_column,
                   kcu.ordinal_position
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name=kcu.constraint_name
             AND tc.table_schema=kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name=tc.constraint_name
             AND ccu.constraint_schema=tc.table_schema
            WHERE tc.table_schema='public' AND tc.constraint_type='FOREIGN KEY'
            ORDER BY kcu.table_name, tc.constraint_name, kcu.ordinal_position
            """
        )
        fk_rows = cur.fetchall()
        cur.execute(
            """
            SELECT relname AS table_name, GREATEST(n_live_tup, 0)::bigint AS estimate
            FROM pg_stat_user_tables
            WHERE schemaname='public'
            """
        )
        estimates = {row["table_name"]: row["estimate"] for row in cur.fetchall()}

    tables: dict[str, dict[str, Any]] = {}
    for row in column_rows:
        table = tables.setdefault(row["table_name"], {
            "name": row["table_name"], "columns": [], "primary_key": [],
            "foreign_keys": [], "referenced_by": [], "row_estimate": 0,
        })
        table["columns"].append({
            "name": row["column_name"],
            "data_type": row["data_type"],
            "udt_name": row["udt_name"],
            "nullable": row["nullable"],
        })
    for row in pk_rows:
        if row["table_name"] in tables:
            tables[row["table_name"]]["primary_key"].append(row["column_name"])

    grouped_fks: dict[tuple[str, str], dict[str, Any]] = {}
    for row in fk_rows:
        key = (row["table_name"], row["constraint_name"])
        item = grouped_fks.setdefault(key, {
            "name": row["constraint_name"], "table": row["table_name"],
            "columns": [], "ref_table": row["ref_table"], "ref_columns": [],
        })
        item["columns"].append(row["column_name"])
        item["ref_columns"].append(row["ref_column"])
    for item in grouped_fks.values():
        if item["table"] not in tables or item["ref_table"] not in tables:
            continue
        tables[item["table"]]["foreign_keys"].append(item)
        tables[item["ref_table"]]["referenced_by"].append(item)
    for name, table in tables.items():
        table["row_estimate"] = int(estimates.get(name, 0))

    ordered = sorted(tables.values(), key=lambda item: item["name"])
    result = {"tables": ordered, "table_map": tables}
    _schema_cache = (now, result)
    return result


def _table_or_error(name: str) -> dict[str, Any]:
    table = load_schema()["table_map"].get(name)
    if not table:
        raise ValueError("Bảng không tồn tại hoặc không thuộc schema public.")
    return table


def _query_value(query: dict[str, list[str]], name: str, default: str = "") -> str:
    return (query.get(name) or [default])[0]


def _int_value(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def table_rows(table_name: str, query: dict[str, list[str]]) -> dict[str, Any]:
    table = _table_or_error(table_name)
    columns = [item["name"] for item in table["columns"]]
    column_types = {item["name"]: item["data_type"] for item in table["columns"]}
    limit = _int_value(_query_value(query, "limit", "50"), 50, 1, 200)
    offset = _int_value(_query_value(query, "offset", "0"), 0, 0, 10_000_000)
    search = _query_value(query, "search").strip()[:200]
    locale = _query_value(query, "locale").strip()[:20]
    null_column = _query_value(query, "null_column")
    null_mode = _query_value(query, "null_mode")
    sort_column = _query_value(query, "sort")
    sort_direction = "DESC" if _query_value(query, "direction").lower() == "desc" else "ASC"

    conditions: list[sql.Composed] = []
    params: list[Any] = []
    text_types = {"text", "character varying", "character", "citext"}
    searchable = [name for name in columns if column_types.get(name) in text_types]
    if search and searchable:
        search_parts = [sql.SQL("{} ILIKE %s").format(sql.Identifier(name)) for name in searchable]
        conditions.append(sql.SQL("(") + sql.SQL(" OR ").join(search_parts) + sql.SQL(")"))
        params.extend([f"%{search}%"] * len(search_parts))
    locale_column = "locale" if "locale" in columns else "language" if "language" in columns else None
    if locale and locale_column:
        conditions.append(sql.SQL("{} = %s").format(sql.Identifier(locale_column)))
        params.append(locale)
    if null_column in columns and null_mode in {"null", "not_null"}:
        operator = sql.SQL("IS NULL") if null_mode == "null" else sql.SQL("IS NOT NULL")
        conditions.append(sql.SQL("{} ").format(sql.Identifier(null_column)) + operator)

    where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
    order_name = sort_column if sort_column in columns else (
        table["primary_key"][0] if table["primary_key"] else columns[0]
    )
    order = sql.SQL(" ORDER BY {} {}").format(sql.Identifier(order_name), sql.SQL(sort_direction))

    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT count(*) AS total FROM {}{}").format(sql.Identifier(table_name), where),
            params,
        )
        total = int(cur.fetchone()["total"])
        cur.execute(
            sql.SQL("SELECT * FROM {}{}{} LIMIT %s OFFSET %s").format(
                sql.Identifier(table_name), where, order,
            ),
            [*params, limit, offset],
        )
        rows = [dict(row) for row in cur.fetchall()]

    primary_key = table["primary_key"]
    output_rows = []
    for row in rows:
        output_rows.append({
            "key": {name: row.get(name) for name in primary_key},
            "values": row,
        })
    return {
        "table": table_name, "columns": table["columns"], "primary_key": primary_key,
        "rows": output_rows, "total": total, "limit": limit, "offset": offset,
        "locale_column": locale_column, "sort": order_name, "direction": sort_direction.lower(),
    }


def table_stats(table_name: str) -> dict[str, Any]:
    table = _table_or_error(table_name)
    now = time.monotonic()
    cached = _stats_cache.get(table_name)
    if cached and now - cached[0] < STATS_CACHE_TTL:
        return cached[1]
    columns = [item["name"] for item in table["columns"]]
    expressions = [sql.SQL("count(*) AS total")]
    expressions.extend(
        sql.SQL("count(*) FILTER (WHERE {} IS NOT NULL) AS {}").format(
            sql.Identifier(name), sql.Identifier(f"filled__{name}"),
        )
        for name in columns
    )
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(expressions), sql.Identifier(table_name),
            )
        )
        raw = dict(cur.fetchone())
    total = int(raw.pop("total"))
    fields = []
    for name in columns:
        filled = int(raw.get(f"filled__{name}", 0))
        fields.append({
            "column": name, "filled": filled, "null": total - filled,
            "percent": round((filled / total * 100), 1) if total else 0,
        })
    result = {"table": table_name, "total": total, "fields": fields}
    _stats_cache[table_name] = (now, result)
    return result


def record_detail(table_name: str, query: dict[str, list[str]]) -> dict[str, Any]:
    table = _table_or_error(table_name)
    raw_key = _query_value(query, "key")
    try:
        key = json.loads(raw_key)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Khóa bản ghi không hợp lệ.") from exc
    primary_key = table["primary_key"]
    if not primary_key or set(key) != set(primary_key):
        raise ValueError("Bảng không có khóa chính hoặc khóa không đầy đủ.")
    conditions = [sql.SQL("{} = %s").format(sql.Identifier(name)) for name in primary_key]
    params = [key[name] for name in primary_key]
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            sql.SQL("SELECT * FROM {} WHERE {} LIMIT 1").format(
                sql.Identifier(table_name), sql.SQL(" AND ").join(conditions),
            ),
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError("Không tìm thấy bản ghi.")
        values = dict(row)
        relations: list[dict[str, Any]] = []
        for fk in table["foreign_keys"]:
            fk_values = [values.get(name) for name in fk["columns"]]
            if any(value is None for value in fk_values):
                count = 0
            else:
                fk_where = [sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fk["ref_columns"]]
                cur.execute(
                    sql.SQL("SELECT count(*) AS n FROM {} WHERE {}").format(
                        sql.Identifier(fk["ref_table"]), sql.SQL(" AND ").join(fk_where),
                    ),
                    fk_values,
                )
                count = int(cur.fetchone()["n"])
            relations.append({
                "direction": "outbound", "table": fk["ref_table"],
                "constraint": fk["name"], "count": count,
            })
        for fk in table["referenced_by"]:
            parent_values = [values.get(name) for name in fk["ref_columns"]]
            if any(value is None for value in parent_values):
                count = 0
            else:
                child_where = [sql.SQL("{} = %s").format(sql.Identifier(name)) for name in fk["columns"]]
                cur.execute(
                    sql.SQL("SELECT count(*) AS n FROM {} WHERE {}").format(
                        sql.Identifier(fk["table"]), sql.SQL(" AND ").join(child_where),
                    ),
                    parent_values,
                )
                count = int(cur.fetchone()["n"])
            relations.append({
                "direction": "inbound", "table": fk["table"],
                "constraint": fk["name"], "count": count,
            })
    return {"table": table_name, "key": key, "values": values, "relations": relations}


def hotel_rows(query: dict[str, list[str]]) -> dict[str, Any]:
    """Return a mentor-friendly, paginated hotel catalogue."""
    limit = _int_value(_query_value(query, "limit", "20"), 20, 1, 60)
    offset = _int_value(_query_value(query, "offset", "0"), 0, 0, 10_000_000)
    search = _query_value(query, "search").strip()[:200]
    locale = _query_value(query, "locale", "vi")
    if locale not in {"vi", "en"}:
        locale = "vi"
    currency = _query_value(query, "currency", "VND")
    if currency not in {"VND", "USD"}:
        currency = "VND"
    star = _query_value(query, "star")
    detail_status = _query_value(query, "status")
    sort_name = _query_value(query, "sort", "recent")

    where_parts = ["TRUE"]
    where_params: list[Any] = []
    if search:
        where_parts.append(
            "(h.trip_hotel_id::text ILIKE %s OR EXISTS ("
            "SELECT 1 FROM hotel_translations sx WHERE sx.hotel_id=h.id "
            "AND (sx.name ILIKE %s OR sx.address ILIKE %s)))"
        )
        term = f"%{search}%"
        where_params.extend([term, term, term])
    if star in {"1", "2", "3", "4", "5"}:
        where_parts.append("h.star_rating = %s")
        where_params.append(int(star))
    # The migration creates name-only EN placeholders for every hotel.  Do not
    # present those rows as crawled English data in the mentor-facing catalogue.
    if locale == "en":
        where_parts.append(
            "EXISTS (SELECT 1 FROM hotel_translations en_ready "
            "WHERE en_ready.hotel_id=h.id AND en_ready.locale='en' "
            "AND en_ready.address IS NOT NULL)"
        )

    has_images = "EXISTS (SELECT 1 FROM hotel_images x WHERE x.hotel_id=h.id)"
    has_amenities = "EXISTS (SELECT 1 FROM hotel_amenities x WHERE x.hotel_id=h.id)"
    has_rooms = "EXISTS (SELECT 1 FROM room_types x WHERE x.hotel_id=h.id)"
    if detail_status == "complete":
        where_parts.append(f"{has_images} AND {has_amenities} AND {has_rooms}")
    elif detail_status == "partial":
        where_parts.append(
            f"({has_images} OR {has_amenities} OR {has_rooms}) "
            f"AND NOT ({has_images} AND {has_amenities} AND {has_rooms})"
        )
    elif detail_status == "missing":
        where_parts.append(f"NOT ({has_images} OR {has_amenities} OR {has_rooms})")

    where_sql = " AND ".join(where_parts)
    order_sql = {
        "rating": "h.review_score DESC NULLS LAST, h.review_count DESC NULLS LAST",
        "reviews": "h.review_count DESC NULLS LAST, h.review_score DESC NULLS LAST",
        "stars": "h.star_rating DESC NULLS LAST, h.review_score DESC NULLS LAST",
        "name": "COALESCE(t.name, '') ASC, h.id ASC",
        "recent": "h.last_seen_at DESC NULLS LAST, h.id DESC",
    }.get(sort_name, "h.last_seen_at DESC NULLS LAST, h.id DESC")

    select_sql = f"""
        SELECT h.id, h.trip_hotel_id, h.url, h.latitude, h.longitude,
               h.star_rating, h.review_score, h.review_count,
               h.first_seen_at, h.last_seen_at,
               t.locale, t.name, t.address, t.description, t.hotel_type,
               image.url AS image_url,
               price.min_price,
               (SELECT count(*) FROM hotel_images x WHERE x.hotel_id=h.id) AS image_count,
               (SELECT count(*) FROM hotel_amenities x WHERE x.hotel_id=h.id) AS amenity_count,
               (SELECT count(*) FROM room_types x WHERE x.hotel_id=h.id) AS room_count,
               (SELECT count(*) FROM hotel_policies x WHERE x.hotel_id=h.id) AS policy_count,
               (SELECT count(*) FROM hotel_nearby_places x WHERE x.hotel_id=h.id) AS nearby_count
        FROM hotels h
        LEFT JOIN LATERAL (
            SELECT ht.locale, ht.name, ht.address, ht.description, ht.hotel_type
            FROM hotel_translations ht
            WHERE ht.hotel_id=h.id AND ht.locale IN (%s, 'vi', 'en')
            ORDER BY CASE WHEN ht.locale=%s THEN 0 WHEN ht.locale='vi' THEN 1 ELSE 2 END
            LIMIT 1
        ) t ON TRUE
        LEFT JOIN LATERAL (
            SELECT hi.url FROM hotel_images hi
            WHERE hi.hotel_id=h.id
            ORDER BY hi.sort_order NULLS LAST, hi.id
            LIMIT 1
        ) image ON TRUE
        LEFT JOIN LATERAL (
            SELECT min(hp.price) AS min_price FROM hotel_prices hp
            WHERE hp.hotel_id=h.id AND hp.currency=%s AND hp.price IS NOT NULL
              AND hp.captured_date=(
                  SELECT max(hp2.captured_date) FROM hotel_prices hp2
                  WHERE hp2.hotel_id=h.id AND hp2.currency=%s
              )
        ) price ON TRUE
        WHERE {where_sql}
        ORDER BY {order_sql}
        LIMIT %s OFFSET %s
    """
    count_sql = f"SELECT count(*) AS total FROM hotels h WHERE {where_sql}"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(count_sql, where_params)
        total = int(cur.fetchone()["total"])
        cur.execute(
            select_sql,
            [locale, locale, currency, currency, *where_params, limit, offset],
        )
        rows = [dict(row) for row in cur.fetchall()]

    for row in rows:
        present = sum(bool(row.get(name)) for name in ("image_count", "amenity_count", "room_count"))
        row["detail_status"] = "complete" if present == 3 else "partial" if present else "missing"
        row["currency"] = currency
        if row.get("description") and len(row["description"]) > 260:
            row["description"] = row["description"][:257].rstrip() + "..."
    return {
        "rows": rows, "total": total, "limit": limit, "offset": offset,
        "locale": locale, "currency": currency,
    }


def hotel_detail(hotel_id: int, locale: str) -> dict[str, Any]:
    if locale not in {"vi", "en"}:
        locale = "vi"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT h.id, h.trip_hotel_id, h.url, h.location_id, h.latitude, h.longitude,
                   h.star_rating, h.review_score, h.review_count,
                   h.first_seen_at, h.last_seen_at,
                   t.locale, t.name, t.address, t.description, t.hotel_type,
                   COALESCE(lt.name, l.name, l.name_en) AS location_name,
                   l.type AS location_type, l.country_code,
                   (SELECT count(*) FROM hotel_images x WHERE x.hotel_id=h.id) AS image_count,
                   (SELECT count(*) FROM hotel_amenities x WHERE x.hotel_id=h.id) AS amenity_count,
                   (SELECT count(*) FROM room_types x WHERE x.hotel_id=h.id) AS room_count,
                   (SELECT count(*) FROM hotel_prices x WHERE x.hotel_id=h.id) AS price_count,
                   (SELECT count(*) FROM hotel_policies x WHERE x.hotel_id=h.id) AS policy_count,
                   (SELECT count(*) FROM hotel_nearby_places x WHERE x.hotel_id=h.id) AS nearby_count
            FROM hotels h
            LEFT JOIN LATERAL (
                SELECT ht.locale, ht.name, ht.address, ht.description, ht.hotel_type
                FROM hotel_translations ht
                WHERE ht.hotel_id=h.id AND ht.locale IN (%s, 'vi', 'en')
                ORDER BY CASE WHEN ht.locale=%s THEN 0 WHEN ht.locale='vi' THEN 1 ELSE 2 END
                LIMIT 1
            ) t ON TRUE
            LEFT JOIN locations l ON l.id=h.location_id
            LEFT JOIN LATERAL (
                SELECT name FROM location_translations
                WHERE location_id=l.id AND locale IN (%s, 'vi', 'en')
                ORDER BY CASE WHEN locale=%s THEN 0 WHEN locale='vi' THEN 1 ELSE 2 END
                LIMIT 1
            ) lt ON TRUE
            WHERE h.id=%s
            """,
            (locale, locale, locale, locale, hotel_id),
        )
        hotel = cur.fetchone()
        if hotel is None:
            raise LookupError("Không tìm thấy khách sạn.")
        cur.execute(
            """
            SELECT locale, name, address, description, hotel_type, source_url,
                   crawled_at, updated_at
            FROM hotel_translations WHERE hotel_id=%s
            ORDER BY CASE locale WHEN 'vi' THEN 0 WHEN 'en' THEN 1 ELSE 2 END
            """,
            (hotel_id,),
        )
        translations = [dict(row) for row in cur.fetchall()]
    result = dict(hotel)
    present = sum(bool(result.get(name)) for name in ("image_count", "amenity_count", "room_count"))
    result["detail_status"] = "complete" if present == 3 else "partial" if present else "missing"
    return {"hotel": result, "translations": translations}


def hotel_section(hotel_id: int, section: str, locale: str) -> dict[str, Any]:
    if locale not in {"vi", "en"}:
        locale = "vi"
    allowed = {"images", "amenities", "rooms", "prices", "policies", "nearby", "raw"}
    if section not in allowed:
        raise ValueError("Phân mục khách sạn không hợp lệ.")
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM hotels WHERE id=%s", (hotel_id,))
        if cur.fetchone() is None:
            raise LookupError("Không tìm thấy khách sạn.")

        if section == "images":
            cur.execute(
                """
                SELECT hi.id, hi.url, hi.category AS source_category, hi.sort_order,
                       c.category_code, c.category_name, c.image_title, c.locale
                FROM hotel_images hi
                LEFT JOIN LATERAL (
                    SELECT hic.category_code, hic.category_name, hic.image_title, hic.locale
                    FROM hotel_image_categories hic
                    WHERE hic.hotel_image_id=hi.id AND hic.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN hic.locale=%s THEN 0 WHEN hic.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) c ON TRUE
                WHERE hi.hotel_id=%s
                ORDER BY hi.sort_order NULLS LAST, hi.id
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "amenities":
            cur.execute(
                """
                SELECT ha.id, ha.amenity_code, ha.free_type, ha.is_highlight,
                       (to_jsonb(ha)->>'is_available')::boolean AS is_available,
                       COALESCE(t.amenity_name, ha.amenity_name) AS amenity_name,
                       COALESCE(t.category, ha.category) AS category,
                       t.fee_label, t.additional_info, t.locale
                FROM hotel_amenities ha
                LEFT JOIN LATERAL (
                    SELECT x.locale, x.amenity_name, x.category, x.fee_label, x.additional_info
                    FROM hotel_amenity_translations x
                    WHERE x.hotel_amenity_id=ha.id AND x.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) t ON TRUE
                WHERE ha.hotel_id=%s
                ORDER BY ha.is_highlight DESC NULLS LAST, COALESCE(t.category, ha.category), ha.id
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "rooms":
            cur.execute(
                """
                SELECT r.id, r.trip_room_id, r.max_occupancy, r.area_sqm,
                       r.bedroom_count, r.bathroom_count, r.bed_count,
                       COALESCE(t.name, r.name) AS name,
                       COALESCE(t.bed_type, r.bed_type) AS bed_type,
                       t.view_name, t.smoking_policy, t.wifi, t.floor_label,
                       t.extra_bed_policy, t.locale
                FROM room_types r
                LEFT JOIN LATERAL (
                    SELECT x.* FROM room_type_translations x
                    WHERE x.room_type_id=r.id AND x.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) t ON TRUE
                WHERE r.hotel_id=%s ORDER BY r.id
                """,
                (locale, locale, hotel_id),
            )
            rooms = [dict(row) for row in cur.fetchall()]
            room_map = {row["id"]: row for row in rooms}
            for room in rooms:
                room["images"] = []
                room["amenities"] = []
            room_ids = list(room_map)
            if room_ids:
                cur.execute(
                    """
                    SELECT room_type_id, url, category_code, sort_order
                    FROM room_images WHERE room_type_id=ANY(%s)
                    ORDER BY room_type_id, sort_order NULLS LAST, id
                    """,
                    (room_ids,),
                )
                for row in cur.fetchall():
                    room_map[row["room_type_id"]]["images"].append(dict(row))
                cur.execute(
                    """
                    SELECT ra.id, ra.room_type_id, ra.amenity_key, ra.amenity_code,
                           ra.category_code, ra.is_highlight, ra.free_type,
                           t.amenity_name, t.category_name, t.additional_info, t.locale
                    FROM room_amenities ra
                    LEFT JOIN LATERAL (
                        SELECT x.locale, x.amenity_name, x.category_name, x.additional_info
                        FROM room_amenity_translations x
                        WHERE x.room_amenity_id=ra.id AND x.locale IN (%s, 'vi', 'en')
                        ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                        LIMIT 1
                    ) t ON TRUE
                    WHERE ra.room_type_id=ANY(%s)
                    ORDER BY ra.room_type_id, ra.is_highlight DESC NULLS LAST, ra.id
                    """,
                    (locale, locale, room_ids),
                )
                for row in cur.fetchall():
                    room_map[row["room_type_id"]]["amenities"].append(dict(row))
            return {"section": section, "items": rooms}

        if section == "prices":
            cur.execute(
                """
                SELECT p.id, p.room_type_id, COALESCE(t.name, r.name) AS room_name,
                       p.check_in, p.check_out, p.price, p.currency, p.tax_included,
                       p.language, p.price_type, p.captured_date, p.captured_at
                FROM hotel_prices p
                LEFT JOIN room_types r ON r.id=p.room_type_id
                LEFT JOIN LATERAL (
                    SELECT x.name FROM room_type_translations x
                    WHERE x.room_type_id=r.id AND x.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) t ON TRUE
                WHERE p.hotel_id=%s
                ORDER BY p.captured_date DESC, p.currency, p.price NULLS LAST
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "policies":
            cur.execute(
                """
                SELECT p.id, p.policy_code, p.sort_order, t.locale, t.title, t.description
                FROM hotel_policies p
                LEFT JOIN LATERAL (
                    SELECT x.locale, x.title, x.description
                    FROM hotel_policy_translations x
                    WHERE x.hotel_policy_id=p.id AND x.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) t ON TRUE
                WHERE p.hotel_id=%s ORDER BY p.sort_order, p.id
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "nearby":
            cur.execute(
                """
                SELECT p.id, p.trip_poi_id, p.category_code, p.poi_type,
                       p.latitude, p.longitude, p.distance_km, p.arrival_type, p.sort_order,
                       t.locale, t.name, t.category_name, t.distance_text, t.description, t.tags
                FROM hotel_nearby_places p
                LEFT JOIN LATERAL (
                    SELECT x.locale, x.name, x.category_name, x.distance_text,
                           x.description, x.tags
                    FROM hotel_nearby_place_translations x
                    WHERE x.nearby_place_id=p.id AND x.locale IN (%s, 'vi', 'en')
                    ORDER BY CASE WHEN x.locale=%s THEN 0 WHEN x.locale='vi' THEN 1 ELSE 2 END
                    LIMIT 1
                ) t ON TRUE
                WHERE p.hotel_id=%s ORDER BY p.category_code, p.sort_order, p.id
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        cur.execute("SELECT raw_json FROM hotels WHERE id=%s", (hotel_id,))
        raw_hotel = cur.fetchone()["raw_json"]
        cur.execute(
            "SELECT locale, raw_json FROM hotel_translations WHERE hotel_id=%s ORDER BY locale",
            (hotel_id,),
        )
        translations = [dict(row) for row in cur.fetchall()]
        return {"section": section, "hotel": raw_hotel, "translations": translations}


class DataViewerHandler(BaseHTTPRequestHandler):
    server_version = "TripDataViewer/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, relative: str) -> None:
        target = (WEB_DIR / relative).resolve()
        if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/api/health":
                with db_connection() as conn, conn.cursor() as cur:
                    cur.execute("SELECT current_database(), now()")
                    database, now = cur.fetchone()
                self.send_json({"ok": True, "database": database, "time": now})
                return
            if path == "/api/schema":
                schema = load_schema(force=_query_value(query, "refresh") == "1")
                self.send_json({"tables": schema["tables"]})
                return
            if path == "/api/hotels":
                self.send_json(hotel_rows(query))
                return
            if path.startswith("/api/hotels/"):
                parts = [part for part in path.removeprefix("/api/hotels/").split("/") if part]
                try:
                    hotel_id = int(parts[0])
                except (IndexError, ValueError) as exc:
                    raise ValueError("ID khách sạn không hợp lệ.") from exc
                locale = _query_value(query, "locale", "vi")
                if len(parts) == 1:
                    self.send_json(hotel_detail(hotel_id, locale))
                elif len(parts) == 2:
                    self.send_json(hotel_section(hotel_id, parts[1], locale))
                else:
                    raise ValueError("Đường dẫn khách sạn không hợp lệ.")
                return
            if path.startswith("/api/table/") and path.endswith("/stats"):
                table_name = path[len("/api/table/"):-len("/stats")].strip("/")
                self.send_json(table_stats(table_name))
                return
            if path.startswith("/api/table/"):
                table_name = path[len("/api/table/"):].strip("/")
                self.send_json(table_rows(table_name, query))
                return
            if path.startswith("/api/record/"):
                table_name = path[len("/api/record/"):].strip("/")
                self.send_json(record_detail(table_name, query))
                return
            if path == "/" or path == "/index.html":
                self.send_static("index.html")
                return
            if path == "/database" or path == "/database.html":
                self.send_static("database.html")
                return
            if path.startswith("/assets/"):
                self.send_static(path.removeprefix("/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except LookupError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except psycopg2.Error as exc:
            message = exc.diag.message_primary if exc.diag else "Lỗi truy vấn PostgreSQL."
            self.send_json({"error": message}, HTTPStatus.INTERNAL_SERVER_ERROR)
        except Exception as exc:  # keep local demo server alive on malformed rows
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only PostgreSQL data viewer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not WEB_DIR.exists():
        raise SystemExit(f"Không tìm thấy thư mục giao diện: {WEB_DIR}")
    server = ThreadingHTTPServer((args.host, args.port), DataViewerHandler)
    print(f"PostgreSQL Data Viewer: http://{args.host}:{args.port}")
    print("Read-only mode. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
