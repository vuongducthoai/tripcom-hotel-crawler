"""Local read-only PostgreSQL data viewer for the crawler database.

Run from the repository root:
    python src/web_app.py

The server intentionally binds to 127.0.0.1 and every database connection is
read-only.  No web framework is required.
"""
from __future__ import annotations

import argparse
import base64
import os
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
import crawl_coverage
from crawl_jobs import RUNNER


WEB_DIR = config.ROOT / "web"
SCHEMA_CACHE_TTL = 30
STATS_CACHE_TTL = 60
# Khách sạn luôn hiện đầu danh sách (dùng cho demo). Đổi bằng biến môi trường
# FEATURED_HOTEL_IDS="134013415,123967146" hoặc để trống để tắt.
FEATURED_HOTEL_IDS = [
    item.strip() for item in os.getenv("FEATURED_HOTEL_IDS", "134013415").split(",")
    if item.strip()
]
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


def cities() -> dict[str, Any]:
    """Các thành phố ĐÃ có khách sạn trong schema v2, kèm số lượng.

    Web không còn gắn cứng TP.HCM: cào hoặc nạp thành phố nào thì thành phố đó
    tự hiện ra ở ô chọn.
    """
    sql = """
        SELECT c.trip_city_id,
               COALESCE(ci_vi.name, ci_en.name, '(chưa có tên)') AS name_vi,
               COALESCE(ci_en.name, ci_vi.name)                  AS name_en,
               COALESCE(co_vi.name, co_en.name)                  AS country_vi,
               COALESCE(co_en.name, co_vi.name)                  AS country_en,
               count(h.id)                                       AS hotel_count
        FROM v2.cities c
        JOIN v2.hotels h        ON h.city_id = c.id
        LEFT JOIN v2.city_i18n ci_vi    ON ci_vi.city_id = c.id AND ci_vi.locale = 'vi'
        LEFT JOIN v2.city_i18n ci_en    ON ci_en.city_id = c.id AND ci_en.locale = 'en'
        LEFT JOIN v2.countries co       ON co.id = c.country_id
        LEFT JOIN v2.country_i18n co_vi ON co_vi.country_id = co.id AND co_vi.locale = 'vi'
        LEFT JOIN v2.country_i18n co_en ON co_en.country_id = co.id AND co_en.locale = 'en'
        GROUP BY c.trip_city_id, ci_vi.name, ci_en.name, co_vi.name, co_en.name
        ORDER BY count(h.id) DESC, 2
    """
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        rows = [dict(row) for row in cur.fetchall()]
    return {"cities": rows, "total": sum(int(r["hotel_count"]) for r in rows)}


def hotel_rows(query: dict[str, list[str]]) -> dict[str, Any]:
    """Danh mục khách sạn, đọc từ schema v2 (đa thành phố, song ngữ)."""
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
    city = _query_value(query, "city").strip()

    where_parts = ["TRUE"]
    where_params: list[Any] = []
    if city.isdigit():
        where_parts.append("c.trip_city_id = %s")
        where_params.append(int(city))
    if search:
        where_parts.append(
            "(h.trip_hotel_id::text ILIKE %s OR EXISTS ("
            "SELECT 1 FROM v2.hotel_i18n sx WHERE sx.hotel_id = h.id "
            "AND (sx.name ILIKE %s OR sx.address ILIKE %s)))"
        )
        term = f"%{search}%"
        where_params.extend([term, term, term])
    if star in {"1", "2", "3", "4", "5"}:
        where_parts.append("h.star_level = %s")
        where_params.append(int(star))
    # Chỉ coi là "có bản ngôn ngữ này" khi thật sự đã cào, không tính bản ghi rỗng.
    where_parts.append(
        "EXISTS (SELECT 1 FROM v2.hotel_i18n rdy WHERE rdy.hotel_id = h.id "
        "AND rdy.locale = %s AND rdy.address IS NOT NULL)"
    )
    where_params.append(locale)

    has_images = "EXISTS (SELECT 1 FROM v2.hotel_images x WHERE x.hotel_id = h.id)"
    has_amenities = "EXISTS (SELECT 1 FROM v2.hotel_amenities x WHERE x.hotel_id = h.id)"
    has_rooms = "EXISTS (SELECT 1 FROM v2.room_types x WHERE x.hotel_id = h.id)"
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
        "rating": "r.rating_overall DESC NULLS LAST, r.review_count DESC NULLS LAST",
        "reviews": "r.review_count DESC NULLS LAST, r.rating_overall DESC NULLS LAST",
        "stars": "h.star_level DESC NULLS LAST, r.rating_overall DESC NULLS LAST",
        "name": "COALESCE(t.name, '') ASC, h.id ASC",
        "recent": "h.updated_at DESC NULLS LAST, h.id DESC",
    }.get(sort_name, "h.updated_at DESC NULLS LAST, h.id DESC")

    from_sql = """
        FROM v2.hotels h
        LEFT JOIN v2.cities c ON c.id = h.city_id
        LEFT JOIN v2.hotel_review_summary r ON r.hotel_id = h.id
        LEFT JOIN LATERAL (
            SELECT hi.locale, hi.name, hi.address, hi.description, hi.hotel_type
            FROM v2.hotel_i18n hi
            WHERE hi.hotel_id = h.id AND hi.locale IN (%s, 'vi', 'en')
            ORDER BY CASE WHEN hi.locale = %s THEN 0 WHEN hi.locale = 'vi' THEN 1 ELSE 2 END
            LIMIT 1
        ) t ON TRUE
    """
    select_sql = f"""
        SELECT h.id, h.trip_hotel_id, h.detail_url AS url, h.latitude, h.longitude,
               h.star_level AS star_rating, h.star_type,
               r.rating_overall AS review_score, r.review_count,
               h.first_seen_at, h.updated_at AS last_seen_at,
               t.locale, t.name, t.address, t.description, t.hotel_type,
               c.trip_city_id,
               COALESCE(ci_pref.name, ci_vi.name, ci_en.name) AS city_name,
               COALESCE(co_pref.name, co_vi.name, co_en.name) AS country_name,
               image.url AS image_url,
               price.min_price,
               (SELECT count(*) FROM v2.hotel_images x WHERE x.hotel_id = h.id) AS image_count,
               (SELECT count(*) FROM v2.hotel_amenities x WHERE x.hotel_id = h.id) AS amenity_count,
               (SELECT count(*) FROM v2.room_types x WHERE x.hotel_id = h.id) AS room_count,
               (SELECT count(*) FROM v2.hotel_policy_sections x WHERE x.hotel_id = h.id) AS policy_count,
               (SELECT count(*) FROM v2.hotel_nearby_places x WHERE x.hotel_id = h.id) AS nearby_count
        {from_sql}
        LEFT JOIN v2.city_i18n ci_pref ON ci_pref.city_id = c.id AND ci_pref.locale = %s
        LEFT JOIN v2.city_i18n ci_vi   ON ci_vi.city_id = c.id AND ci_vi.locale = 'vi'
        LEFT JOIN v2.city_i18n ci_en   ON ci_en.city_id = c.id AND ci_en.locale = 'en'
        LEFT JOIN v2.countries co         ON co.id = c.country_id
        LEFT JOIN v2.country_i18n co_pref ON co_pref.country_id = co.id AND co_pref.locale = %s
        LEFT JOIN v2.country_i18n co_vi   ON co_vi.country_id = co.id AND co_vi.locale = 'vi'
        LEFT JOIN v2.country_i18n co_en   ON co_en.country_id = co.id AND co_en.locale = 'en'
        LEFT JOIN LATERAL (
            SELECT hi.url FROM v2.hotel_images hi
            WHERE hi.hotel_id = h.id
            ORDER BY hi.is_cover DESC, hi.sort_order NULLS LAST, hi.id
            LIMIT 1
        ) image ON TRUE
        LEFT JOIN LATERAL (
            SELECT min(ps.min_price) AS min_price FROM v2.hotel_price_snapshots ps
            WHERE ps.hotel_id = h.id AND ps.currency = %s AND ps.min_price IS NOT NULL
              AND ps.captured_date = (
                  SELECT max(ps2.captured_date) FROM v2.hotel_price_snapshots ps2
                  WHERE ps2.hotel_id = h.id AND ps2.currency = %s
              )
        ) price ON TRUE
        WHERE {where_sql}
        ORDER BY CASE WHEN h.trip_hotel_id::text = ANY(%s) THEN 0 ELSE 1 END, {order_sql}
        LIMIT %s OFFSET %s
    """
    count_sql = f"SELECT count(*) AS total {from_sql} WHERE {where_sql}"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(count_sql, [locale, locale, *where_params])
        total = int(cur.fetchone()["total"])
        cur.execute(
            select_sql,
            [locale, locale, locale, locale, currency, currency, *where_params,
             FEATURED_HOTEL_IDS, limit, offset],
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
        "locale": locale, "currency": currency, "city": city,
    }


def hotel_detail(hotel_id: int, locale: str) -> dict[str, Any]:
    """Thông tin đầu trang chi tiết, đọc từ schema v2."""
    if locale not in {"vi", "en"}:
        locale = "vi"
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT h.id, h.trip_hotel_id, h.detail_url AS url, h.city_id AS location_id,
                   h.latitude, h.longitude,
                   h.star_level AS star_rating, h.star_type, h.room_count AS hotel_room_count,
                   h.open_year, h.renovated_year,
                   r.rating_overall AS review_score, r.review_count,
                   h.first_seen_at, h.updated_at AS last_seen_at,
                   t.locale, t.name, t.local_name, t.address, t.description, t.hotel_type,
                   t.zone_name, t.traffic_desc,
                   c.trip_city_id,
                   COALESCE(ci_pref.name, ci_vi.name, ci_en.name) AS location_name,
                   COALESCE(co_pref.name, co_vi.name, co_en.name) AS country_name,
                   co.iso2 AS country_code,
                   (SELECT count(*) FROM v2.hotel_images x WHERE x.hotel_id = h.id) AS image_count,
                   (SELECT count(*) FROM v2.hotel_amenities x WHERE x.hotel_id = h.id) AS amenity_count,
                   (SELECT count(*) FROM v2.room_types x WHERE x.hotel_id = h.id) AS room_count,
                   (SELECT count(*) FROM v2.room_offers o JOIN v2.room_types rt ON rt.id = o.room_type_id
                     WHERE rt.hotel_id = h.id) AS price_count,
                   (SELECT count(*) FROM v2.hotel_policy_sections x WHERE x.hotel_id = h.id) AS policy_count,
                   (SELECT count(*) FROM v2.hotel_nearby_places x WHERE x.hotel_id = h.id) AS nearby_count
            FROM v2.hotels h
            LEFT JOIN v2.hotel_review_summary r ON r.hotel_id = h.id
            LEFT JOIN v2.cities c ON c.id = h.city_id
            LEFT JOIN v2.city_i18n ci_pref ON ci_pref.city_id = c.id AND ci_pref.locale = %s
            LEFT JOIN v2.city_i18n ci_vi   ON ci_vi.city_id = c.id AND ci_vi.locale = 'vi'
            LEFT JOIN v2.city_i18n ci_en   ON ci_en.city_id = c.id AND ci_en.locale = 'en'
            LEFT JOIN v2.countries co         ON co.id = c.country_id
            LEFT JOIN v2.country_i18n co_pref ON co_pref.country_id = co.id AND co_pref.locale = %s
            LEFT JOIN v2.country_i18n co_vi   ON co_vi.country_id = co.id AND co_vi.locale = 'vi'
            LEFT JOIN v2.country_i18n co_en   ON co_en.country_id = co.id AND co_en.locale = 'en'
            LEFT JOIN LATERAL (
                SELECT hi.locale, hi.name, hi.local_name, hi.address, hi.description,
                       hi.hotel_type, hi.zone_name, hi.traffic_desc
                FROM v2.hotel_i18n hi
                WHERE hi.hotel_id = h.id AND hi.locale IN (%s, 'vi', 'en')
                ORDER BY CASE WHEN hi.locale = %s THEN 0 WHEN hi.locale = 'vi' THEN 1 ELSE 2 END
                LIMIT 1
            ) t ON TRUE
            WHERE h.id = %s
            """,
            (locale, locale, locale, locale, hotel_id),
        )
        hotel = cur.fetchone()
        if hotel is None:
            raise LookupError("Không tìm thấy khách sạn.")
        cur.execute(
            """
            SELECT locale, name, local_name, address, description, hotel_type
            FROM v2.hotel_i18n WHERE hotel_id = %s
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
    """Từng tab của trang chi tiết, đọc từ schema v2."""
    if locale not in {"vi", "en"}:
        locale = "vi"
    allowed = {"images", "amenities", "rooms", "prices", "policies", "nearby", "raw"}
    if section not in allowed:
        raise ValueError("Phân mục khách sạn không hợp lệ.")
    with db_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT 1 FROM v2.hotels WHERE id = %s", (hotel_id,))
        if cur.fetchone() is None:
            raise LookupError("Không tìm thấy khách sạn.")

        if section == "images":
            cur.execute(
                """
                SELECT hi.id, hi.url, hi.uploader AS source_category, hi.sort_order,
                       hi.is_cover, hi.trip_category_id AS category_code,
                       COALESCE(ci_pref.name, ci_vi.name, ci_en.name) AS category_name,
                       NULL::text AS image_title, %s AS locale
                FROM v2.hotel_images hi
                LEFT JOIN v2.image_category_i18n ci_pref
                       ON ci_pref.trip_category_id = hi.trip_category_id AND ci_pref.locale = %s
                LEFT JOIN v2.image_category_i18n ci_vi
                       ON ci_vi.trip_category_id = hi.trip_category_id AND ci_vi.locale = 'vi'
                LEFT JOIN v2.image_category_i18n ci_en
                       ON ci_en.trip_category_id = hi.trip_category_id AND ci_en.locale = 'en'
                WHERE hi.hotel_id = %s
                ORDER BY hi.is_cover DESC, hi.sort_order NULLS LAST, hi.id
                """,
                (locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "amenities":
            cur.execute(
                """
                SELECT ha.trip_amenity_id AS id, ha.trip_amenity_id AS amenity_code,
                       ha.fee AS free_type, ha.is_popular AS is_highlight, ha.is_available,
                       COALESCE(ai_pref.name, ai_vi.name, ai_en.name) AS amenity_name,
                       COALESCE(ac_pref.name, ac_vi.name, ac_en.name) AS category,
                       d.fee_label, d.details AS additional_info, %s AS locale
                FROM v2.hotel_amenities ha
                JOIN v2.amenities a ON a.trip_amenity_id = ha.trip_amenity_id
                LEFT JOIN v2.amenity_i18n ai_pref
                       ON ai_pref.trip_amenity_id = a.trip_amenity_id AND ai_pref.locale = %s
                LEFT JOIN v2.amenity_i18n ai_vi
                       ON ai_vi.trip_amenity_id = a.trip_amenity_id AND ai_vi.locale = 'vi'
                LEFT JOIN v2.amenity_i18n ai_en
                       ON ai_en.trip_amenity_id = a.trip_amenity_id AND ai_en.locale = 'en'
                LEFT JOIN v2.amenity_category_i18n ac_pref
                       ON ac_pref.trip_category_id = a.trip_category_id AND ac_pref.locale = %s
                LEFT JOIN v2.amenity_category_i18n ac_vi
                       ON ac_vi.trip_category_id = a.trip_category_id AND ac_vi.locale = 'vi'
                LEFT JOIN v2.amenity_category_i18n ac_en
                       ON ac_en.trip_category_id = a.trip_category_id AND ac_en.locale = 'en'
                LEFT JOIN v2.hotel_amenity_details d
                       ON d.hotel_id = ha.hotel_id AND d.trip_amenity_id = ha.trip_amenity_id
                      AND d.locale = %s
                WHERE ha.hotel_id = %s
                ORDER BY ha.is_popular DESC, 7, ha.trip_amenity_id
                """,
                (locale, locale, locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "rooms":
            cur.execute(
                """
                SELECT r.id, r.trip_room_id, r.max_adults AS max_occupancy, r.area_sqm,
                       r.bedroom_count, r.bathroom_count, r.bed_count,
                       r.smoking AS smoking_policy, r.wifi, r.extra_bed,
                       COALESCE(t_pref.name, t_vi.name, t_en.name) AS name,
                       COALESCE(t_pref.bed_summary, t_vi.bed_summary, t_en.bed_summary) AS bed_type,
                       COALESCE(t_pref.view_text, t_vi.view_text, t_en.view_text) AS view_name,
                       COALESCE(t_pref.area_text, t_vi.area_text, t_en.area_text) AS area_text,
                       COALESCE(t_pref.guest_text, t_vi.guest_text, t_en.guest_text) AS guest_text,
                       COALESCE(t_pref.extra_bed_text, t_vi.extra_bed_text, t_en.extra_bed_text)
                           AS extra_bed_policy,
                       NULL::text AS floor_label, %s AS locale
                FROM v2.room_types r
                LEFT JOIN v2.room_type_i18n t_pref ON t_pref.room_type_id = r.id AND t_pref.locale = %s
                LEFT JOIN v2.room_type_i18n t_vi   ON t_vi.room_type_id = r.id AND t_vi.locale = 'vi'
                LEFT JOIN v2.room_type_i18n t_en   ON t_en.room_type_id = r.id AND t_en.locale = 'en'
                WHERE r.hotel_id = %s
                ORDER BY r.sort_order NULLS LAST, r.id
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
                    SELECT room_type_id, url, NULL::int AS category_code, sort_order
                    FROM v2.room_images WHERE room_type_id = ANY(%s)
                    ORDER BY room_type_id, sort_order NULLS LAST, url
                    """,
                    (room_ids,),
                )
                for row in cur.fetchall():
                    room_map[row["room_type_id"]]["images"].append(dict(row))
                cur.execute(
                    """
                    SELECT ra.room_type_id, ra.trip_amenity_id AS id,
                           ra.trip_amenity_id AS amenity_code, a.trip_category_id AS category_code,
                           ra.is_highlight, ra.fee AS free_type,
                           COALESCE(ai_pref.name, ai_vi.name, ai_en.name) AS amenity_name,
                           COALESCE(ac_pref.name, ac_vi.name, ac_en.name) AS category_name,
                           NULL::jsonb AS additional_info, %s AS locale
                    FROM v2.room_amenities ra
                    JOIN v2.amenities a ON a.trip_amenity_id = ra.trip_amenity_id
                    LEFT JOIN v2.amenity_i18n ai_pref
                           ON ai_pref.trip_amenity_id = a.trip_amenity_id AND ai_pref.locale = %s
                    LEFT JOIN v2.amenity_i18n ai_vi
                           ON ai_vi.trip_amenity_id = a.trip_amenity_id AND ai_vi.locale = 'vi'
                    LEFT JOIN v2.amenity_i18n ai_en
                           ON ai_en.trip_amenity_id = a.trip_amenity_id AND ai_en.locale = 'en'
                    LEFT JOIN v2.amenity_category_i18n ac_pref
                           ON ac_pref.trip_category_id = a.trip_category_id AND ac_pref.locale = %s
                    LEFT JOIN v2.amenity_category_i18n ac_vi
                           ON ac_vi.trip_category_id = a.trip_category_id AND ac_vi.locale = 'vi'
                    LEFT JOIN v2.amenity_category_i18n ac_en
                           ON ac_en.trip_category_id = a.trip_category_id AND ac_en.locale = 'en'
                    WHERE ra.room_type_id = ANY(%s)
                    ORDER BY ra.room_type_id, ra.is_highlight DESC, ra.trip_amenity_id
                    """,
                    (locale, locale, locale, room_ids),
                )
                for row in cur.fetchall():
                    room_map[row["room_type_id"]]["amenities"].append(dict(row))
            return {"section": section, "items": rooms}

        if section == "prices":
            # Mỗi dòng là một gói giá của một loại phòng, kèm giá từng tiền tệ.
            cur.execute(
                """
                SELECT o.id, o.room_type_id,
                       COALESCE(t_pref.name, t_vi.name, t_en.name) AS room_name,
                       o.check_in, o.check_out, p.price_per_night AS price, p.currency,
                       p.total_price, p.taxes_fees,
                       (p.taxes_fees IS NOT NULL) AS tax_included,
                       %s AS language,
                       COALESCE(oi_pref.title, oi_vi.title, oi_en.title) AS price_type,
                       o.captured_date, p.captured_at,
                       o.breakfast_included, o.free_cancellation, o.is_sold_out
                FROM v2.room_offers o
                JOIN v2.room_types r ON r.id = o.room_type_id
                JOIN v2.room_offer_prices p ON p.offer_id = o.id
                LEFT JOIN v2.room_type_i18n t_pref ON t_pref.room_type_id = r.id AND t_pref.locale = %s
                LEFT JOIN v2.room_type_i18n t_vi   ON t_vi.room_type_id = r.id AND t_vi.locale = 'vi'
                LEFT JOIN v2.room_type_i18n t_en   ON t_en.room_type_id = r.id AND t_en.locale = 'en'
                LEFT JOIN v2.room_offer_i18n oi_pref ON oi_pref.offer_id = o.id AND oi_pref.locale = %s
                LEFT JOIN v2.room_offer_i18n oi_vi   ON oi_vi.offer_id = o.id AND oi_vi.locale = 'vi'
                LEFT JOIN v2.room_offer_i18n oi_en   ON oi_en.offer_id = o.id AND oi_en.locale = 'en'
                WHERE r.hotel_id = %s
                ORDER BY o.captured_date DESC, p.currency, p.price_per_night NULLS LAST
                """,
                (locale, locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        if section == "policies":
            cur.execute(
                """
                SELECT ps.section_code AS policy_code, ps.sort_order, ps.locale, ps.title,
                       string_agg(
                           CASE WHEN COALESCE(pl.label, '') = '' THEN pl.text
                                ELSE pl.label || ': ' || pl.text END,
                           E'\n' ORDER BY pl.line_no) AS description
                FROM v2.hotel_policy_sections ps
                LEFT JOIN v2.hotel_policy_lines pl
                       ON pl.hotel_id = ps.hotel_id AND pl.locale = ps.locale
                      AND pl.section_code = ps.section_code
                WHERE ps.hotel_id = %s AND ps.locale = %s
                GROUP BY ps.section_code, ps.sort_order, ps.locale, ps.title
                ORDER BY ps.sort_order, ps.section_code
                """,
                (hotel_id, locale),
            )
            items = [dict(row) for row in cur.fetchall()]
            for index, row in enumerate(items):
                row["id"] = index + 1
            return {"section": section, "items": items}

        if section == "nearby":
            cur.execute(
                """
                SELECT np.place_id AS id, pl.trip_poi_id, np.group_code AS category_code,
                       pl.poi_type, pl.latitude, pl.longitude, np.distance_km,
                       np.travel_mode AS arrival_type, np.sort_order,
                       %s AS locale,
                       COALESCE(pi_pref.name, pi_vi.name, pi_en.name) AS name,
                       npi.group_name AS category_name, npi.distance_text,
                       COALESCE(pi_pref.kind, pi_vi.kind, pi_en.kind) AS description,
                       NULL::jsonb AS tags
                FROM v2.hotel_nearby_places np
                JOIN v2.places pl ON pl.id = np.place_id
                LEFT JOIN v2.place_i18n pi_pref ON pi_pref.place_id = pl.id AND pi_pref.locale = %s
                LEFT JOIN v2.place_i18n pi_vi   ON pi_vi.place_id = pl.id AND pi_vi.locale = 'vi'
                LEFT JOIN v2.place_i18n pi_en   ON pi_en.place_id = pl.id AND pi_en.locale = 'en'
                LEFT JOIN v2.hotel_nearby_place_i18n npi
                       ON npi.hotel_id = np.hotel_id AND npi.place_id = np.place_id
                      AND npi.locale = %s
                WHERE np.hotel_id = %s
                ORDER BY np.group_code, np.sort_order, np.place_id
                """,
                (locale, locale, locale, hotel_id),
            )
            return {"section": section, "items": [dict(row) for row in cur.fetchall()]}

        # raw: v2 không lưu raw_json trong DB (raw nằm ở output/details/raw),
        # nên trả về bản ghi đã chuẩn hóa để vẫn xem được cấu trúc.
        cur.execute(
            """
            SELECT to_jsonb(h) AS hotel,
                   (SELECT jsonb_agg(to_jsonb(i)) FROM v2.hotel_i18n i WHERE i.hotel_id = h.id)
                       AS translations,
                   (SELECT to_jsonb(p) FROM v2.hotel_policies p WHERE p.hotel_id = h.id)
                       AS policies
            FROM v2.hotels h WHERE h.id = %s
            """,
            (hotel_id,),
        )
        row = cur.fetchone()
        return {
            "section": section,
            "hotel": row["hotel"],
            "translations": row["translations"] or [],
            "policies": row["policies"],
        }


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

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Chỉ nhận 2 lệnh: bắt đầu một job có sẵn, hoặc dừng job đang chạy.

        Không có đường nào để web truyền lệnh tuỳ ý xuống shell — xem
        crawl_jobs.JOB_SPECS.
        """
        path = unquote(urlsplit(self.path).path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise ValueError("Body phải là JSON hợp lệ.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Body phải là một object JSON.")

            if path == "/api/crawl/start":
                job = RUNNER.start(str(payload.get("job") or ""), payload.get("params") or {})
                self.send_json({"ok": True, "job": job})
                return
            if path == "/api/crawl/stop":
                self.send_json({"ok": True, "job": RUNNER.stop()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

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
            if path == "/api/cities":
                self.send_json(cities())
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
            if path == "/crawl" or path == "/crawl.html":
                self.send_static("crawl.html")
                return
            if path == "/api/crawl/coverage":
                locale = _query_value(query, "locale", "vi")
                with db_connection() as conn:
                    self.send_json({
                        "coverage": crawl_coverage.coverage(conn, locale),
                        "cities": crawl_coverage.cities(conn),
                    })
                return
            if path == "/api/crawl/jobs":
                self.send_json(RUNNER.status())
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
