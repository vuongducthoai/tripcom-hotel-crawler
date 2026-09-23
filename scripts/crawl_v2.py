"""Cào lại cho schema v2 theo lô: Việt + Anh CÙNG ngày ở, rồi kiểm tra + nạp v2.

    python scripts/crawl_v2.py --plan                          # xem kế hoạch, không cào
    python scripts/crawl_v2.py --ids 134013415 110585808        # thử vài hotel
    python scripts/crawl_v2.py --lot-size 20 --max-lots 1       # thử 1 lô nhỏ
    python scripts/crawl_v2.py --city-id 1356                   # một thành phố (Đà Nẵng)
    python scripts/crawl_v2.py                                  # toàn bộ hotel trong DB
    python scripts/crawl_v2.py --list-file api_hotels_<cityId>_….json   # thành phố nước ngoài
    python scripts/crawl_v2.py --only vi                        # chỉ tiếng Việt (tiếng Anh cào sau)
    python scripts/crawl_v2.py --only en                        # bù tiếng Anh, tự dùng ngày của bản Việt

Mỗi lô (mặc định 50 hotel):
    1. Cào bản Việt  (src/crawl_detail.py --ids-file … --checkin NGÀY)
    2. Cào bản Anh   (cùng NGÀY — để gói giá VND và USD ghép được với nhau)
    3. Kiểm tra + nạp v2 cho từng thứ tiếng (src/db/v2_loader.py --ids-file …)
    4. Nghỉ rồi sang lô sau

Hotel đã "xong v2" thì bỏ qua: cả hai bản raw đều có khối hotelDetailResponse,
cào thành công và cùng ngày nhận phòng. Chạy lại lệnh bao nhiêu lần cũng được.

Tự dừng khi crawl_detail báo bị chặn / trình duyệt chết, hoặc khi v2_loader
báo chốt chặn cả đợt không đạt — sau khi đã xử lý nốt phần vừa cào.

File này chỉ GỌI các script có sẵn, không sửa gì. Danh sách hotel lấy từ bảng
hotels cũ (schema public), hoặc từ file danh sách của crawl_api.py (--list-file)
— dùng cách này cho thành phố nước ngoài, khỏi phải nạp vào DB cũ.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import raw_store

MARKETS = (("vi-VN", "VND"), ("en-US", "USD"))
DETAIL_BLOCK_URL = "embedded:hotel-detail-response"   # giống crawl_detail.DETAIL_BLOCK_URL
STOP_SIGNALS = (
    ("Dừng an toàn", "Trip.com chặn liên tiếp"),
    ("Trình duyệt đã đóng", "trình duyệt Chromium đã đóng"),
    ("liên tiếp không ra phòng", "Trip.com đang giới hạn API phòng"),
)


class StopAll(Exception):
    pass


def default_stay() -> tuple[str, str]:
    """Giống crawl_detail.default_stay(): thứ Hai tuần sau nữa, 1 đêm."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=14)
    return monday.isoformat(), (monday + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- chọn hotel
def hotels_in_scope(city_id: int | None) -> list[str]:
    import psycopg2
    sql = "SELECT h.trip_hotel_id FROM public.hotels h"
    params: tuple = ()
    if city_id is not None:
        sql += (" JOIN public.locations l ON l.id = h.location_id"
                " WHERE l.trip_location_id IN (%s, %s)")
        params = (f"city:{city_id}", str(city_id))
    else:
        sql += " WHERE h.trip_hotel_id IS NOT NULL"
    sql += " ORDER BY h.id"
    with psycopg2.connect(config.dsn()) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [str(row[0]) for row in cur.fetchall() if row[0]]


def hotels_in_list(list_file: str) -> tuple[list[str], Path]:
    path = Path(list_file)
    if not path.exists():
        path = config.DATA_DIR / path.name
    if not path.exists():
        raise SystemExit(f"Không tìm thấy file danh sách: {list_file}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = [str(h.get("trip_hotel_id")) for h in payload.get("hotels") or [] if h.get("trip_hotel_id")]
    return list(dict.fromkeys(ids)), path


def raw_state(hotel: str, locale: str, currency: str) -> tuple[bool, str | None]:
    """(raw có khối hotelDetailResponse và cào thành công?, ngày nhận phòng)."""
    path = config.OUTPUT_DIR / "details" / "raw" / locale / currency / f"{hotel}.json"
    try:
        dump = raw_store.read(path)
    except Exception:
        return False, None
    normalized = dump.get("normalized") or {}
    has_block = any(p.get("url") == DETAIL_BLOCK_URL for p in dump.get("responses") or [])
    return bool(has_block and normalized.get("success")), normalized.get("check_in")


def done_v2(hotel: str) -> bool:
    (vi_ok, vi_in), (en_ok, en_in) = (raw_state(hotel, *m) for m in MARKETS)
    return vi_ok and en_ok and vi_in == en_in


def done_market(hotel: str, market: tuple[str, str]) -> bool:
    return raw_state(hotel, *market)[0]


# ---------------------------------------------------------------- gọi script có sẵn
def run(argv: list[str], label: str, ok_codes=(0,)) -> tuple[int, list[str]]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")  # in log ngay, không đợi đầy bộ đệm
    print(f"\n    $ python {' '.join(argv)}", flush=True)
    process = subprocess.Popen([sys.executable, *argv], cwd=str(ROOT), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines: list[str] = []
    assert process.stdout
    for line in process.stdout:
        lines.append(line.rstrip())
        print(f"    │ {line.rstrip()}", flush=True)
    process.wait()
    if process.returncode not in ok_codes:
        raise StopAll(f"{label} thoát với mã {process.returncode}")
    return process.returncode, lines


def crawl(ids_file: Path, locale: str, currency: str, checkin: str | None, checkout: str | None,
          workers: int, list_file: Path | None) -> str | None:
    source = ["--file", str(list_file)] if list_file else ["--from-db"]
    # checkin=None: không ép ngày — crawl_detail tự dùng lại ngày ở của bản thứ
    # tiếng kia cho TỪNG hotel (nếu ngày đó còn ở tương lai), để ghép gói giá.
    dates = ["--checkin", checkin, "--checkout", checkout] if checkin else []
    _, log = run(["src/crawl_detail.py", *source, "--locale", locale, "--currency", currency,
                  "--ids-file", str(ids_file.relative_to(ROOT)), "--workers", str(workers),
                  *dates], f"crawl_detail {locale}")
    return next((why for marker, why in STOP_SIGNALS if any(marker in line for line in log)), None)


def load(ids_file: Path, locale: str, validate_only: bool) -> bool:
    argv = ["src/db/v2_loader.py", "--locale", locale, "--ids-file", str(ids_file.relative_to(ROOT))]
    if validate_only:
        argv.append("--validate-only")
    code, _ = run(argv, f"v2_loader {locale}", ok_codes=(0, 2))
    return code == 0


# ---------------------------------------------------------------- chương trình chính
def main(args) -> None:
    checkin = args.checkin or default_stay()[0]
    checkout = args.checkout or (datetime.fromisoformat(checkin) + timedelta(days=1)).date().isoformat()
    list_file = None
    if args.list_file:
        scope, list_file = hotels_in_list(args.list_file)
        if args.ids:
            wanted = {str(h) for h in args.ids}
            scope = [h for h in scope if h in wanted]
    elif args.ids:
        scope = [str(h) for h in args.ids]
    else:
        scope = hotels_in_scope(args.city_id)
    markets = [m for m in MARKETS if not args.only or m[0].startswith(args.only)]
    if args.only:
        todo = [h for h in scope if args.redo or not done_market(h, markets[0])]
    else:
        todo = [h for h in scope if args.redo or not done_v2(h)]
    # --only en mà không chỉ ngày: mỗi hotel tự dùng ngày của bản tiếng Việt.
    crawl_in, crawl_out = (None, None) if (args.only == "en" and not args.checkin) else (checkin, checkout)
    lots = [todo[i:i + args.lot_size] for i in range(0, len(todo), args.lot_size)]
    if args.max_lots:
        lots = lots[:args.max_lots]

    label = {"vi": "tiếng Việt", "en": "tiếng Anh"}.get(args.only, "cả hai thứ tiếng")
    print(f"Phạm vi {len(scope)} hotel · đã xong ({label}) {len(scope) - len(todo)} · cần cào {len(todo)}")
    if crawl_in:
        print(f"Ngày ở: {crawl_in} → {crawl_out}")
    else:
        print("Ngày ở: lấy theo bản tiếng Việt của từng hotel (nếu ngày đó đã qua thì dùng ngày mặc định)")
    print(f"Chạy {len(lots)} lô × tối đa {args.lot_size} hotel "
          f"({'chỉ kiểm tra, không nạp' if args.validate_only else 'kiểm tra rồi nạp v2'})")
    if args.plan or not lots:
        print("\n(--plan: chưa cào gì)" if args.plan else "\nKhông còn hotel nào cần cào.")
        return
    print("Nhắc: đừng chạy crawl_detail/crawl_api nào khác cùng lúc — dùng chung trình duyệt.")

    batch_dir = config.OUTPUT_DIR / "batches_v2"
    batch_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        for number, lot in enumerate(lots, 1):
            print(f"\n=== Lô {number}/{len(lots)} · {len(lot)} hotel ===")
            ids_file = batch_dir / f"{stamp}_lot{number:03d}.txt"
            ids_file.write_text("".join(f"{h}\n" for h in lot), encoding="utf-8")
            stop = None
            for locale, currency in markets:
                stop = crawl(ids_file, locale, currency, crawl_in, crawl_out, args.workers, list_file)
                if stop:
                    break
            # Kể cả khi phải dừng, vẫn kiểm tra + nạp phần đã cào được.
            gate_ok = all([load(ids_file, locale, args.validate_only) for locale, _ in markets])
            if stop:
                raise StopAll(stop)
            if not gate_ok:
                raise StopAll("v2_loader báo chốt chặn không đạt — xem output/v2_reports/")
            if number < len(lots):
                print(f"\nNghỉ {args.pause}s trước lô sau…", flush=True)
                time.sleep(args.pause)
    except StopAll as why:
        print(f"\nDỪNG: {why}")
        print("Nếu bị chặn: nghỉ vài tiếng (tốt nhất qua đêm) rồi chạy lại đúng lệnh này —")
        print("hotel đã xong sẽ tự được bỏ qua.")
        sys.exit(1)
    print("\nXong tất cả các lô.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ids", nargs="+", help="chỉ các hotel này (thay cho đọc DB)")
    ap.add_argument("--city-id", type=int, help="chỉ một thành phố trong DB cũ, vd 1356 (Đà Nẵng)")
    ap.add_argument("--list-file", help="file danh sách của crawl_api.py (output/data/api_hotels_*.json)")
    ap.add_argument("--lot-size", type=int, default=50, help="số hotel mỗi lô (mặc định 50)")
    ap.add_argument("--max-lots", type=int, help="chỉ chạy N lô đầu (để thử)")
    ap.add_argument("--workers", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--pause", type=int, default=60, help="nghỉ giữa các lô, giây (mặc định 60)")
    ap.add_argument("--checkin", help="YYYY-MM-DD; mặc định thứ Hai tuần sau nữa")
    ap.add_argument("--checkout", help="YYYY-MM-DD; mặc định checkin + 1 ngày")
    ap.add_argument("--only", choices=("vi", "en"),
                    help="chỉ cào một thứ tiếng; --only en tự dùng ngày ở của bản tiếng Việt")
    ap.add_argument("--validate-only", action="store_true", help="cào + kiểm tra, KHÔNG nạp v2")
    ap.add_argument("--redo", action="store_true", help="cào lại cả hotel đã xong v2")
    ap.add_argument("--plan", action="store_true", help="chỉ in kế hoạch")
    main(ap.parse_args())
