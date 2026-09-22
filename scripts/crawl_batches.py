"""Cào theo lô, xong lô nào chắc lô đó: đủ cả tiếng Việt lẫn tiếng Anh.

    python scripts/crawl_batches.py --plan                   # chỉ xem kế hoạch, không cào
    python scripts/crawl_batches.py                          # toàn bộ hotel trong DB
    python scripts/crawl_batches.py --city-id 1356           # một thành phố (Đà Nẵng)
    python scripts/crawl_batches.py --lot-size 20 --max-lots 1   # chạy thử 1 lô nhỏ

Mỗi lô (mặc định 100 hotel):
    1. Hỏi DB: hotel nào trong lô còn thiếu mô tả / tiện nghi / phòng, ở ngôn ngữ nào
    2. Cào VI đúng các hotel thiếu VI, rồi nạp DB
    3. Cào EN đúng các hotel thiếu EN, rồi nạp DB
    4. Hỏi lại DB; còn thiếu thì cào bù (mặc định thêm 1 vòng)
    5. Ghi báo cáo lô vào output/batches/report.jsonl, nghỉ, sang lô sau

Hotel đã đủ cả hai thứ tiếng thì bỏ qua hẳn — nên chạy lại lệnh bao nhiêu
lần cũng được, nó tự làm tiếp phần còn thiếu.

File này KHÔNG sửa code nào khác. Nó chỉ gọi các script có sẵn:
    src/crawl_detail.py --ids-file   (cào)
    src/db/detail_loader.py          (nạp, chế độ mặc định: chỉ thêm/cập nhật, không xóa)
    scripts/find_missing.py          (hỏi DB xem thiếu gì — chỉ đọc)

Tự dừng cả chương trình khi crawl_detail báo bị chặn hoặc trình duyệt chết,
sau khi đã nạp nốt phần vừa cào.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import raw_store
from find_missing import missing_from_db

MARKETS = (
    ("vi-VN", "VND", "vi", "VI"),
    ("en-US", "USD", "en", "EN"),
)
DEFAULT_FIELDS = ("description", "amenities", "rooms")

# crawl_detail in ra các dòng này khi phải dừng — gặp là dừng cả chương trình.
STOP_SIGNALS = (
    ("Dừng an toàn", "Trip.com chặn liên tiếp"),
    ("Trình duyệt đã đóng", "trình duyệt Chromium đã đóng"),
    ("liên tiếp không ra phòng", "Trip.com đang giới hạn API phòng"),
)
MANIFEST_LINE = re.compile(r"Nạp DB: python src/db/detail_loader\.py (\S+\.json)")


class StopAll(Exception):
    """Dừng toàn bộ chương trình (bị chặn, trình duyệt chết, script lỗi)."""


# ---------------------------------------------------------------- DB (chỉ đọc)
def hotels_in_scope(city_id: int | None) -> list[str]:
    import psycopg2
    sql = "SELECT h.trip_hotel_id FROM hotels h"
    params: tuple = ()
    if city_id is not None:
        # db/location_upsert.py lưu mã thành phố dạng "city:1356"; nhận cả
        # dạng trần "1356" phòng dữ liệu cũ.
        sql += (" JOIN locations l ON l.id = h.location_id"
                " WHERE l.trip_location_id IN (%s, %s)")
        params = (f"city:{city_id}", str(city_id))
    else:
        sql += " WHERE h.trip_hotel_id IS NOT NULL"
    sql += " ORDER BY h.id"
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [str(row[0]) for row in cur.fetchall() if row[0]]


def missing_map(fields: list[str]) -> dict[str, dict[str, set[str]]]:
    """{lang: {field: {trip_hotel_id,...}}} — toàn DB, mỗi truy vấn vài chục ms."""
    result: dict[str, dict[str, set[str]]] = {}
    for locale, _, lang, _ in MARKETS:
        result[lang] = {}
        for field in fields:
            ids, _, _ = missing_from_db(locale, [field])
            result[lang][field] = set(ids)
    return result


def sold_out(hotel: str, locale: str, currency: str) -> bool:
    """Raw mới nhất nói Trip.com trả lời 'hết phòng' — cào lại cũng vô ích."""
    path = config.OUTPUT_DIR / "details" / "raw" / locale / currency / f"{hotel}.json"
    try:
        return bool((raw_store.read(path).get("normalized") or {}).get("rooms_sold_out"))
    except Exception:
        return False


def lot_gaps(lot: list[str], gaps: dict, fields: list[str]) -> dict[str, dict[str, set[str]]]:
    """Thu hẹp bản đồ thiếu về đúng các hotel trong lô, bỏ phòng đã 'hết phòng'."""
    members = set(lot)
    out: dict[str, dict[str, set[str]]] = {}
    for locale, currency, lang, _ in MARKETS:
        out[lang] = {}
        for field in fields:
            ids = gaps[lang][field] & members
            if field == "rooms":
                ids = {h for h in ids if not sold_out(h, locale, currency)}
            out[lang][field] = ids
    return out


def needs(lot_gap: dict, lang: str) -> set[str]:
    return set().union(*lot_gap[lang].values()) if lot_gap[lang] else set()


# ---------------------------------------------------------------- gọi script có sẵn
def run(argv: list[str], label: str) -> list[str]:
    """Chạy một script con, in log ra màn hình theo thời gian thực, trả về log."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    print(f"\n    $ python {' '.join(argv)}", flush=True)
    process = subprocess.Popen(
        [sys.executable, *argv], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout
    for line in process.stdout:
        text = line.rstrip()
        lines.append(text)
        print(f"    │ {text}", flush=True)
    process.wait()
    if process.returncode != 0:
        raise StopAll(f"{label} thoát với mã {process.returncode}")
    return lines


def crawl_and_load(ids: set[str], locale: str, currency: str, tag: str,
                   args: argparse.Namespace, batch_dir: Path) -> None:
    ids_file = batch_dir / f"{tag}.txt"
    ids_file.write_text("".join(f"{h}\n" for h in sorted(ids)), encoding="utf-8")

    argv = ["src/crawl_detail.py", "--from-db", "--locale", locale, "--currency", currency,
            "--ids-file", str(ids_file.relative_to(ROOT)), "--workers", str(args.workers)]
    if args.checkin:
        argv += ["--checkin", args.checkin]
    if args.checkout:
        argv += ["--checkout", args.checkout]
    log = run(argv, f"crawl_detail {locale}")

    stop_reason = next((why for marker, why in STOP_SIGNALS
                        if any(marker in line for line in log)), None)
    manifest = next((m.group(1) for line in reversed(log)
                     if (m := MANIFEST_LINE.search(line))), None)

    # Kể cả khi phải dừng, vẫn nạp phần đã cào được trước.
    if manifest:
        run(["src/db/detail_loader.py", manifest], f"detail_loader {locale}")
    else:
        print("    (crawl_detail không tạo manifest — không có gì để nạp)")

    if stop_reason:
        raise StopAll(stop_reason)


# ---------------------------------------------------------------- chương trình chính
def fmt_gap(gap: dict, fields: list[str]) -> str:
    parts = []
    for _, _, lang, name in MARKETS:
        items = [f"{f} {len(gap[lang][f])}" for f in fields if gap[lang][f]]
        parts.append(f"{name}: " + (", ".join(items) if items else "đủ"))
    return " | ".join(parts)


def main(args: argparse.Namespace) -> None:
    fields = list(dict.fromkeys(args.field))
    batch_dir = config.OUTPUT_DIR / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    report = batch_dir / "report.jsonl"

    scope = hotels_in_scope(args.city_id)
    if not scope:
        raise SystemExit(
            "Không có hotel nào trong phạm vi."
            + (f" Thành phố {args.city_id} đã nạp danh sách vào DB chưa "
               "(crawl_api.py rồi db/loader.py)?" if args.city_id else "")
        )

    gaps = missing_map(fields)
    todo = [h for h in scope if any(h in gaps[lang][f] for _, _, lang, _ in MARKETS
                                     for f in fields)]
    lots = [todo[i:i + args.lot_size] for i in range(0, len(todo), args.lot_size)]
    if args.max_lots:
        lots = lots[:args.max_lots]

    print("=" * 72)
    print(f"Phạm vi: {len(scope)} hotel"
          + (f" (thành phố {args.city_id})" if args.city_id else " (toàn DB)"))
    print(f"Kiểm tra: {', '.join(fields)} × VI + EN")
    print(f"Đã đủ cả hai thứ tiếng: {len(scope) - len(todo)} | cần làm: {len(todo)}")
    print(f"→ {len(lots)} lô × tối đa {args.lot_size} hotel, cào bù tối đa {args.retries} vòng/lô")
    print("=" * 72)
    if args.plan or not lots:
        whole = lot_gaps(todo, gaps, fields)
        print("Thiếu theo mục:", fmt_gap(whole, fields))
        if args.plan:
            print("\n(--plan: chỉ xem kế hoạch, chưa cào gì)")
        return

    print("Nhắc: đừng chạy crawl_detail/crawl_api nào khác cùng lúc — dùng chung trình duyệt.")
    started = time.time()
    totals = {"complete": 0, "incomplete": 0}

    try:
        for number, lot in enumerate(lots, 1):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            print(f"\n{'─' * 72}\nLÔ {number}/{len(lots)} — {len(lot)} hotel")
            gap = lot_gaps(lot, gaps, fields)
            print(f"  Thiếu lúc đầu: {fmt_gap(gap, fields)}")

            for round_no in range(0, args.retries + 1):
                if round_no:
                    print(f"\n  ↻ Cào bù vòng {round_no}: {fmt_gap(gap, fields)}")
                for locale, currency, lang, name in MARKETS:
                    ids = needs(gap, lang)
                    if not ids:
                        continue
                    print(f"\n  [{name}] cào {len(ids)} hotel")
                    crawl_and_load(ids, locale, currency,
                                   f"lot{number:03d}_{stamp}_{lang}_r{round_no}",
                                   args, batch_dir)
                gaps = missing_map(fields)          # hỏi lại DB sau khi nạp
                gap = lot_gaps(lot, gaps, fields)
                if not any(needs(gap, lang) for _, _, lang, _ in MARKETS):
                    break

            incomplete = set().union(*(needs(gap, lang) for _, _, lang, _ in MARKETS))
            complete = len(lot) - len(incomplete)
            totals["complete"] += complete
            totals["incomplete"] += len(incomplete)
            with report.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "lot": number, "finished_at": datetime.now().isoformat(timespec="seconds"),
                    "hotels": len(lot), "complete": complete,
                    "still_missing": {lang: {f: sorted(gap[lang][f]) for f in fields}
                                      for _, _, lang, _ in MARKETS},
                }, ensure_ascii=False) + "\n")
            print(f"\n  ✓ Lô {number}: {complete}/{len(lot)} hotel đủ cả hai thứ tiếng"
                  + (f" | còn thiếu: {fmt_gap(gap, fields)}" if incomplete else ""))

            if number < len(lots) and args.pause:
                print(f"  Nghỉ {args.pause}s trước lô sau...")
                time.sleep(args.pause)

    except StopAll as stop:
        print(f"\n{'=' * 72}\nDỪNG: {stop}")
        print("  Phần đã cào đã được nạp vào DB. Nghỉ vài tiếng rồi chạy lại đúng lệnh cũ —")
        print("  hotel đã đủ sẽ tự bỏ qua, nó làm tiếp từ chỗ còn thiếu.")
    except KeyboardInterrupt:
        print("\n\nĐã dừng tay (Ctrl+C). Chạy lại đúng lệnh cũ để làm tiếp.")

    minutes = (time.time() - started) / 60
    print(f"\n{'=' * 72}")
    print(f"Xong {totals['complete'] + totals['incomplete']} hotel trong {minutes:.0f} phút: "
          f"{totals['complete']} đủ cả hai thứ tiếng, {totals['incomplete']} còn thiếu.")
    print(f"Báo cáo từng lô: {report.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--city-id", type=int, help="chỉ làm một thành phố, vd 1356 (Đà Nẵng)")
    ap.add_argument("--lot-size", type=int, default=100, help="số hotel mỗi lô (mặc định 100)")
    ap.add_argument("--max-lots", type=int, help="chỉ chạy N lô đầu (để thử)")
    ap.add_argument("--retries", type=int, default=1, help="số vòng cào bù mỗi lô (mặc định 1)")
    ap.add_argument("--field", nargs="+", default=list(DEFAULT_FIELDS),
                    choices=("description", "amenities", "rooms", "policies",
                             "nearby_places", "images"),
                    help="mục phải đủ (mặc định: description amenities rooms)")
    ap.add_argument("--workers", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--pause", type=int, default=60, help="nghỉ giữa các lô, giây (mặc định 60)")
    ap.add_argument("--checkin", help="YYYY-MM-DD, chuyển thẳng cho crawl_detail")
    ap.add_argument("--checkout", help="YYYY-MM-DD, chuyển thẳng cho crawl_detail")
    ap.add_argument("--plan", action="store_true", help="chỉ in kế hoạch, không cào")
    parsed = ap.parse_args()
    if parsed.lot_size < 1 or parsed.retries < 0:
        raise SystemExit("--lot-size phải ≥ 1 và --retries phải ≥ 0.")
    main(parsed)
