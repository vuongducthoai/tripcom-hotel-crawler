"""Xem dữ liệu nào đã nạp vào PostgreSQL, dữ liệu nào còn nằm trên đĩa.

    python scripts/import_status.py

Script CHỈ ĐỌC — không ghi gì vào DB, không gọi mạng. Nó so ba nguồn:

  1. File raw trong output/details/raw/<locale>/<currency>/  (đã cào về máy)
  2. File manifest hotel_details_*.json trong output/data/   (đã gom lại)
  3. Bảng crawl_runs trong PostgreSQL                        (đã nạp vào DB)

rồi in ra đúng những lệnh cần chạy để nạp phần còn thiếu.

Đọc manifest chỉ lấy phần đầu file (vài KB) nên không tốn RAM, dù file to
hàng trăm MB.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import psycopg2

import config
import raw_store

HEAD_BYTES = 4096
MARKETS = (("vi-VN", "VND", "vi"), ("en-US", "USD", "en"))


def manifest_head(path: Path) -> dict:
    """Lấy các trường ở đầu manifest mà không phải đọc cả file."""
    with path.open("rb") as handle:
        head = handle.read(HEAD_BYTES).decode("utf-8", errors="replace")
    info: dict = {}
    for key in ("locale", "currency", "crawled_at", "source_overview"):
        found = re.search(rf'"{key}"\s*:\s*"([^"]*)"', head)
        if found:
            info[key] = found.group(1)
    for key in ("count", "success_count"):
        found = re.search(rf'"{key}"\s*:\s*(\d+)', head)
        if found:
            info[key] = int(found.group(1))
    found = re.search(r'"complete"\s*:\s*(true|false)', head)
    if found:
        info["complete"] = found.group(1) == "true"
    return info


def imported_targets(conn) -> dict[str, str]:
    """Tên file đã nạp -> trạng thái lần nạp gần nhất."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (target) target, status
            FROM crawl_runs
            WHERE target LIKE 'detail:%'
            ORDER BY target, started_at DESC
        """)
        return {row[0].removeprefix("detail:"): row[1] for row in cur.fetchall()}


def rows_in_db(conn, locale: str) -> dict[str, int]:
    """Vài con số cho thấy ngôn ngữ này đã thực sự vào DB đến đâu."""
    queries = {
        "hotel_translations có mô tả": """
            SELECT count(*) FROM hotel_translations
            WHERE locale=%s AND description IS NOT NULL AND btrim(description) <> ''
        """,
        "hotel có loại phòng": """
            SELECT count(DISTINCT r.hotel_id) FROM room_types r
            JOIN room_type_translations t ON t.room_type_id=r.id AND t.locale=%s
        """,
        "hotel có chính sách": """
            SELECT count(DISTINCT p.hotel_id) FROM hotel_policies p
            JOIN hotel_policy_translations t ON t.hotel_policy_id=p.id AND t.locale=%s
        """,
    }
    out = {}
    with conn.cursor() as cur:
        for label, sql in queries.items():
            cur.execute(sql, (locale,))
            out[label] = int(cur.fetchone()[0])
    return out


def main() -> None:
    raw_root = config.OUTPUT_DIR / "details" / "raw"

    with psycopg2.connect(config.dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM hotels")
            total_hotels = int(cur.fetchone()[0])
        done = imported_targets(conn)

        print("=" * 78)
        print(f"TỔNG KHÁCH SẠN TRONG DB: {total_hotels}")
        print("=" * 78)

        # --- 1. file raw đã cào về máy --------------------------------------
        print("\n[1] FILE RAW ĐÃ CÀO VỀ MÁY")
        raw_counts = {}
        for locale, currency, lang in MARKETS:
            directory = raw_root / locale / currency
            count = len(list(raw_store.iter_raw_files(directory))) if directory.exists() else 0
            raw_counts[lang] = count
            print(f"    {locale}/{currency}: {count} hotel")
        legacy = [p for p in raw_root.glob("*.json*") if p.is_file()]
        if legacy:
            print(f"    (thư mục gốc, bản VI đời cũ): {len(legacy)} file")

        # --- 2. manifest trên đĩa -------------------------------------------
        print("\n[2] FILE MANIFEST TRONG output/data/  (✓ = đã nạp vào DB)")
        manifests = sorted(config.DATA_DIR.glob("hotel_details_*.json"),
                           key=lambda p: p.stat().st_mtime)
        pending: dict[str, list[Path]] = {"vi": [], "en": []}
        if not manifests:
            print("    (không có file nào)")
        for path in manifests:
            size_mb = path.stat().st_size / 1024 / 1024
            if not path.stat().st_size:
                print(f"    [rỗng] {path.name}  — file hỏng, bỏ qua")
                continue
            info = manifest_head(path)
            lang = "en" if str(info.get("locale", "")).lower().startswith("en") else "vi"
            status = done.get(path.name)
            mark = "✓" if status == "success" else ("!" if status else " ")
            note = "" if status == "success" else (f"  <-- nạp dở ({status})" if status
                                                   else "  <-- CHƯA NẠP")
            if status != "success":
                pending[lang].append(path)
            print(f"    {mark} {path.name}")
            print(f"        {lang} · {info.get('count', '?')} hotel "
                  f"(thành công {info.get('success_count', '?')}) · {size_mb:,.0f} MB"
                  f"{'' if info.get('complete') else ' · chưa chạy xong'}{note}")

        # --- 3. thực tế trong DB --------------------------------------------
        print("\n[3] THỰC TẾ ĐÃ VÀO DB")
        for _, _, lang in MARKETS:
            print(f"    [{lang}]")
            for label, value in rows_in_db(conn, lang).items():
                gap = raw_counts.get(lang, 0) - value
                hint = f"  (raw có {raw_counts.get(lang, 0)} → lệch {gap})" if gap > 0 else ""
                print(f"        {label:30} {value:5}{hint}")

    # --- 4. việc cần làm -----------------------------------------------------
    print("\n" + "=" * 78)
    print("VIỆC CẦN LÀM")
    print("=" * 78)
    todo = False
    for locale, currency, lang in MARKETS:
        files = pending[lang]
        if not files:
            continue
        todo = True
        newest = files[-1]
        print(f"\n[{lang}] còn {len(files)} manifest chưa nạp, mới nhất: {newest.name}")
        print("    Các manifest chồng lấn nhau nên ĐỪNG nạp lần lượt từng file.")
        print("    Gom toàn bộ raw thành một file rồi nạp một lần:")
        print(f"        python scripts/reparse_details.py --locale {locale} --currency {currency}")
        print(f"        python src/db/detail_loader.py --locale {locale} --currency {currency} --dry-run")
        print(f"        python src/db/detail_loader.py --locale {locale} --currency {currency}")
    if not todo:
        print("\nKhông có manifest nào chờ nạp.")
    print("\nSau khi nạp xong, kiểm tra lại:")
    print("        python scripts/import_status.py")
    print("        python scripts/audit_data.py")


if __name__ == "__main__":
    main()
