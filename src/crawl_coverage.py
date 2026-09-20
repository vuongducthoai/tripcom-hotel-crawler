"""Thống kê độ phủ dữ liệu: chỗ nào đã crawl, chỗ nào còn thiếu, bao nhiêu %.

Dùng chung logic với scripts/gap_report.sql nhưng trả JSON cho giao diện web.
"""
from __future__ import annotations

from typing import Any

# (nhãn, bảng dữ liệu, cột khoá ngoại tới hotels, bảng dịch, cột khoá, có tách ngôn ngữ)
SECTIONS = (
    ("Ảnh khách sạn", "hotel_images", "hotel_id", None, None),
    ("Tiện nghi khách sạn", "hotel_amenities", "hotel_id",
     "hotel_amenity_translations", "hotel_amenity_id"),
    ("Loại phòng", "room_types", "hotel_id",
     "room_type_translations", "room_type_id"),
    ("Chính sách", "hotel_policies", "hotel_id",
     "hotel_policy_translations", "hotel_policy_id"),
    ("Địa điểm lân cận", "hotel_nearby_places", "hotel_id",
     "hotel_nearby_place_translations", "nearby_place_id"),
)


def _scalar(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(row[0] if not isinstance(row, dict) else list(row.values())[0]) if row else 0


def coverage(conn, locale: str = "vi") -> dict[str, Any]:
    """Độ phủ theo một ngôn ngữ ('vi' hoặc 'en')."""
    if locale not in {"vi", "en"}:
        raise ValueError("locale chỉ nhận 'vi' hoặc 'en'.")

    with conn.cursor() as cur:
        total = _scalar(cur, "SELECT count(*) FROM hotels")
        rows = []

        # Tên/mô tả nằm ở hotel_translations, đếm riêng.
        for label, column in (("Tên khách sạn", "name"),
                              ("Địa chỉ", "address"),
                              ("Mô tả", "description")):
            have = _scalar(cur, f"""
                SELECT count(DISTINCT hotel_id) FROM hotel_translations
                WHERE locale=%s AND {column} IS NOT NULL AND btrim({column}) <> ''
            """, (locale,))
            rows.append({"label": label, "have": have, "missing": total - have,
                         "percent": round(100 * have / total, 1) if total else 0.0})

        for label, table, fk, trans_table, trans_fk in SECTIONS:
            if trans_table:
                have = _scalar(cur, f"""
                    SELECT count(DISTINCT base.{fk})
                    FROM {table} base
                    JOIN {trans_table} tr ON tr.{trans_fk} = base.id
                    WHERE tr.locale = %s
                """, (locale,))
            else:
                # Ảnh dùng chung cho mọi ngôn ngữ (URL giống nhau).
                have = _scalar(cur, f"SELECT count(DISTINCT {fk}) FROM {table}")
            rows.append({"label": label, "have": have, "missing": total - have,
                         "percent": round(100 * have / total, 1) if total else 0.0})

        # Giá phòng tách theo ngôn ngữ/thị trường.
        have = _scalar(cur, """
            SELECT count(DISTINCT hotel_id) FROM hotel_prices
            WHERE language = %s AND price_type = 'room'
        """, (locale,))
        rows.append({"label": "Giá phòng", "have": have, "missing": total - have,
                     "percent": round(100 * have / total, 1) if total else 0.0})

    overall = round(sum(r["percent"] for r in rows) / len(rows), 1) if rows else 0.0
    return {"locale": locale, "total_hotels": total, "overall_percent": overall,
            "sections": sorted(rows, key=lambda r: r["percent"])}


def cities(conn) -> list[dict[str, Any]]:
    """Số khách sạn theo thành phố, để biết tỉnh nào đã làm, tỉnh nào chưa."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.trip_location_id, COALESCE(t.name, l.name) AS name,
                   count(h.id) AS hotels
            FROM locations l
            LEFT JOIN hotels h ON h.location_id = l.id
            LEFT JOIN location_translations t
              ON t.location_id = l.id AND t.locale = 'vi'
            GROUP BY l.trip_location_id, COALESCE(t.name, l.name)
            ORDER BY hotels DESC
        """)
        return [{"trip_location_id": row[0], "name": row[1], "hotels": int(row[2])}
                for row in cur.fetchall()]
