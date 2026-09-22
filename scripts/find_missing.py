"""Lập danh sách khách sạn còn thiếu một loại dữ liệu, để cào lại đúng chỗ đó.

    python scripts/find_missing.py --locale vi-VN --field description amenities
    python scripts/find_missing.py --locale en-US --field description
    python scripts/find_missing.py --locale en-US --field rooms --source raw

Mặc định đọc từ PostgreSQL (--source db): khớp đúng con số trên tab
"Crawl & Theo dõi", vì DB còn có dữ liệu từ repair_hotel_amenities.py mà
raw không có. --source raw thì chỉ đọc file raw trên đĩa, không cần DB.
Chỉ ĐỌC, không ghi gì. Nhiều --field = hotel thiếu BẤT KỲ trường nào.

Kết quả ghi ra output/missing_<locale>_<field>.txt (mỗi dòng một id):

    python src/crawl_detail.py --from-db --locale en-US --currency USD \\
        --ids-file output/missing_enUS_description.txt --workers 2

Với --field rooms, khách sạn Trip.com đã trả lời dứt khoát "hết phòng" sẽ
KHÔNG bị liệt kê (cào lại cùng ngày cũng không ra), trừ khi thêm
--include-sold-out.
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config
import raw_store

FIELDS = ("description", "rooms", "amenities", "policies", "nearby_places", "images")


# Cùng logic với src/crawl_coverage.py để số liệu khớp tab web.
DB_MISSING_SQL = {
    "description": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (
            SELECT 1 FROM hotel_translations t
            WHERE t.hotel_id = h.id AND t.locale = %(lang)s
              AND t.description IS NOT NULL AND btrim(t.description) <> '')
    """,
    "amenities": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (
            SELECT 1 FROM hotel_amenities a
            JOIN hotel_amenity_translations tr ON tr.hotel_amenity_id = a.id
            WHERE a.hotel_id = h.id AND tr.locale = %(lang)s)
    """,
    "rooms": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (
            SELECT 1 FROM room_types r
            JOIN room_type_translations tr ON tr.room_type_id = r.id
            WHERE r.hotel_id = h.id AND tr.locale = %(lang)s)
    """,
    "policies": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (
            SELECT 1 FROM hotel_policies p
            JOIN hotel_policy_translations tr ON tr.hotel_policy_id = p.id
            WHERE p.hotel_id = h.id AND tr.locale = %(lang)s)
    """,
    "nearby_places": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (
            SELECT 1 FROM hotel_nearby_places p
            JOIN hotel_nearby_place_translations tr ON tr.nearby_place_id = p.id
            WHERE p.hotel_id = h.id AND tr.locale = %(lang)s)
    """,
    "images": """
        SELECT h.trip_hotel_id FROM hotels h
        WHERE NOT EXISTS (SELECT 1 FROM hotel_images i WHERE i.hotel_id = h.id)
    """,
}


def missing_from_db(locale: str, fields: list[str]) -> tuple[list[str], dict, int]:
    import psycopg2
    lang = "en" if locale.startswith("en") else "vi"
    per_field: dict[str, set[str]] = {}
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM hotels")
        total = int(cur.fetchone()[0])
        for field in fields:
            cur.execute(DB_MISSING_SQL[field], {"lang": lang})
            per_field[field] = {str(row[0]) for row in cur.fetchall() if row[0]}
    union = sorted(set().union(*per_field.values()), key=lambda x: int(x) if x.isdigit() else 0)
    return union, {f"thiếu {k}": len(v) for k, v in per_field.items()}, total


def raw_dirs(locale: str, currency: str) -> list[Path]:
    root = config.OUTPUT_DIR / "details" / "raw"
    dirs = [root / locale / currency]
    if locale == "vi-VN" and currency == "VND":
        dirs.append(root)  # raw VI đời cũ nằm ngay thư mục gốc
    return [d for d in dirs if d.exists()]


def missing_from_raw(locale: str, currency: str, fields: list[str],
                     include_sold_out: bool) -> tuple[list[str], dict, int]:
    newest: dict[str, tuple[float, Path]] = {}
    for directory in raw_dirs(locale, currency):
        for path in raw_store.iter_raw_files(directory):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue  # crawl đang chạy vừa thay file này
            hotel = raw_store.hotel_id(path)
            if hotel not in newest or mtime > newest[hotel][0]:
                newest[hotel] = (mtime, path)
    if not newest:
        raise SystemExit(f"Không có raw nào cho {locale}/{currency}.")

    missing: list[str] = []
    reasons: collections.Counter = collections.Counter()
    total = len(newest)
    for index, (hotel, (_, path)) in enumerate(sorted(newest.items()), 1):
        if index % 500 == 0:
            print(f"  đã xem {index}/{total}...", flush=True)
        try:
            normalized = raw_store.read(path).get("normalized") or {}
        except Exception:
            reasons["không đọc được raw"] += 1
            missing.append(hotel)
            continue
        lacking = []
        for field in fields:
            if normalized.get(field):
                continue
            if field == "rooms" and normalized.get("rooms_sold_out") and not include_sold_out:
                reasons["hết phòng (bỏ qua)"] += 1
                continue
            lacking.append(field)
        for field in lacking:
            reasons[f"thiếu {field}"] += 1
        if lacking:
            missing.append(hotel)
    return missing, dict(reasons), total


def main(args: argparse.Namespace) -> None:
    locale = args.locale
    currency = (args.currency or ("USD" if locale.startswith("en") else "VND")).upper()
    fields = list(dict.fromkeys(args.field))  # bỏ trùng, giữ thứ tự

    if args.source == "db":
        missing, reasons, total = missing_from_db(locale, fields)
        where = "trong DB"
    else:
        missing, reasons, total = missing_from_raw(
            locale, currency, fields, args.include_sold_out
        )
        where = "có raw"

    token = locale.replace("-", "")
    label = "_".join(fields)
    out = Path(args.out) if args.out else config.OUTPUT_DIR / f"missing_{token}_{label}.txt"
    out.write_text(
        f"# {len(missing)} hotel thiếu {' hoặc '.join(fields)} "
        f"({locale}/{currency}, nguồn {args.source})\n"
        + "".join(f"{hotel}\n" for hotel in missing),
        encoding="utf-8",
    )

    print(f"\n{locale}/{currency}: {total} hotel {where}")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {count:5}  {reason}")
    print(f"\n→ {len(missing)} hotel (thiếu ít nhất một mục) ghi vào {out}")
    if missing:
        try:
            rel = out.relative_to(config.ROOT)
        except ValueError:
            rel = out
        print("\nCào lại đúng các hotel này:")
        print(f"  python src/crawl_detail.py --from-db --locale {locale} --currency {currency} "
              f"--ids-file {rel} --workers 2")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--locale", required=True, choices=("vi-VN", "en-US"))
    ap.add_argument("--currency", help="mặc định USD cho en-US, VND cho vi-VN")
    ap.add_argument("--field", nargs="+", default=["description"], choices=FIELDS,
                    help="một hoặc nhiều mục; hotel thiếu BẤT KỲ mục nào sẽ được liệt kê")
    ap.add_argument("--source", choices=("db", "raw"), default="db",
                    help="db (mặc định, khớp tab web) hoặc raw (không cần DB)")
    ap.add_argument("--include-sold-out", action="store_true",
                    help="với --source raw --field rooms: liệt kê cả hotel Trip.com báo hết phòng")
    ap.add_argument("--out", help="file id đầu ra (mặc định output/missing_<locale>_<field>.txt)")
    main(ap.parse_args())
