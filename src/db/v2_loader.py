"""Nạp raw Trip.com vào schema v2 — có kiểm tra dữ liệu 4 lớp trước khi ghi.

    python src/db/v2_loader.py --locale vi-VN --validate-only   # chỉ kiểm tra, không ghi DB
    python src/db/v2_loader.py --locale vi-VN                   # kiểm tra rồi mới nạp
    python src/db/v2_loader.py --locale en-US --ids 134013415   # vài khách sạn
    python src/db/v2_loader.py --locale en-US --limit 50        # thử 50 khách sạn đầu

Quy trình:
  Lượt 1 — KIỂM TRA toàn bộ raw (không ghi gì):
    lớp 1  response thật hay bị chặn, đúng ngôn ngữ/tiền tệ không
    lớp 2  từng bản ghi qua model Pydantic (src/v2/models.py)
    lớp 3  gói giá ↔ loại phòng, trùng khóa, bản Anh ↔ bản Việt cùng ngày
    lớp 4  so sánh cả đợt với lần nạp trước → không đạt thì DỪNG (trừ khi --force)
  Lượt 2 — GHI: mỗi khách sạn một transaction; PostgreSQL từ chối (CHECK…) thì
    rollback riêng khách sạn đó, ghi lý do, chạy tiếp.

Kết quả:
  * output/v2_reports/<thời điểm>_<locale>.json  (+ .md tóm tắt)
  * khi nạp thật: v2.load_runs (1 dòng) và v2.load_rejects (từng lỗi/cảnh báo)

Danh mục quy tắc và mức lỗi/cảnh báo: src/v2/rules.py (bảng RULES).
Khối hotelDetailResponse (sao, tọa độ, chính sách): lấy trong raw nếu crawler đã
lưu; nếu chưa, thử output/probe_v2/<locale>_<id>.json.gz do probe_detail_v2.py lưu.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import raw_store
from v2.extract import PARSER_VERSION, build_bundle, pair_check, short_locale
from v2.rules import ERROR, GATE, RULES, WARNING, Issues

REPORT_DIR = config.OUTPUT_DIR / "v2_reports"
PROBE_DIR = config.OUTPUT_DIR / "probe_v2"
OTHER = {"vi-VN": ("en-US", "USD"), "en-US": ("vi-VN", "VND")}


# ------------------------------------------------------------------ tìm raw
def raw_dirs(locale: str, currency: str) -> list[Path]:
    root = config.OUTPUT_DIR / "details" / "raw"
    dirs = [root / locale / currency]
    if locale == "vi-VN" and currency == "VND":
        dirs.append(root)  # raw VI đời cũ nằm ngay thư mục gốc
    return [d for d in dirs if d.exists()]


def newest_raw(locale: str, currency: str) -> dict[str, Path]:
    newest: dict[str, tuple[float, Path]] = {}
    for directory in raw_dirs(locale, currency):
        for path in raw_store.iter_raw_files(directory):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            hotel = raw_store.hotel_id(path)
            if not hotel.isdigit():   # bản lưu lần cào hỏng, vd 123.failed.20260920_085629
                continue
            if hotel not in newest or mtime > newest[hotel][0]:
                newest[hotel] = (mtime, path)
    return {hotel: path for hotel, (_, path) in newest.items()}


def probe_detail(locale: str, hotel_id: str) -> dict | None:
    path = PROBE_DIR / f"{locale}_{hotel_id}.json.gz"
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle).get("hotelDetailResponse")
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def process(hotel_id: str, path: Path, locale: str, currency: str, other: dict[str, Path] | None,
            pair: bool):
    """Kiểm tra một khách sạn: (bundle | None, issues)."""
    rel = str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path)
    try:
        dump = raw_store.read(path)
    except Exception as exc:
        issues = Issues(hotel_id, short_locale(locale), rel)
        issues.add("raw_unreadable", "hotel", value=str(exc)[:200])
        return None, issues
    bundle, issues = build_bundle(dump, raw_locale=locale, currency=currency, raw_path=rel,
                                  file_hotel_id=hotel_id, detail=probe_detail(locale, hotel_id))
    if bundle is not None and pair and other is not None:
        other_locale, other_currency = OTHER[locale]
        other_bundle = None
        if hotel_id in other:
            try:
                other_bundle, _ = build_bundle(raw_store.read(other[hotel_id]), raw_locale=other_locale,
                                               currency=other_currency, file_hotel_id=hotel_id)
            except Exception:
                other_bundle = None
        pair_check(bundle, other_bundle, issues)
    return bundle, issues


# ------------------------------------------------------------------ thống kê + chốt chặn
class Stats:
    def __init__(self):
        self.files = 0
        self.ok = 0
        self.rejected = 0
        self.blocked = 0
        self.sections = collections.Counter()
        self.rule_counts = collections.Counter()
        self.fixed = collections.Counter()
        self.rows = collections.Counter()

    def add(self, bundle, issues: Issues) -> None:
        self.files += 1
        for item in issues.items:
            self.rule_counts[item.rule] += 1
        self.fixed.update(issues.fixed)
        if bundle is None:
            self.rejected += 1
            if any(i.rule == "blocked" for i in issues.items):
                self.blocked += 1
            return
        self.ok += 1
        for name, present in bundle.sections().items():
            if present:
                self.sections[name] += 1
        for name in ("images", "hotel_amenities", "policy_sections", "rooms", "offers", "review_tags", "nearby"):
            self.rows[name] += len(getattr(bundle, name))

    def pct(self, section: str) -> float:
        return round(100 * self.sections[section] / self.ok, 1) if self.ok else 0.0

    def as_dict(self) -> dict:
        return {
            "files": self.files, "hotels_ok": self.ok, "hotels_rejected": self.rejected, "blocked": self.blocked,
            "pct": {name: self.pct(name) for name in ("detail", "description", "images", "amenities", "policies",
                                                       "rooms", "offers", "sold_out", "reviews", "nearby")},
            "rows": dict(self.rows), "rules": dict(self.rule_counts.most_common()),
            "fixed": dict(self.fixed.most_common()),
        }


def previous_stats(conn, locale: str) -> dict | None:
    """Thống kê lần nạp thành công gần nhất: ưu tiên DB, không có DB thì đọc báo cáo cũ."""
    if conn is not None:
        with conn.cursor() as cur:
            cur.execute("""SELECT stats FROM v2.load_runs
                           WHERE locale = %s AND mode = 'load' AND status = 'done'
                           ORDER BY started_at DESC LIMIT 1""", (short_locale(locale),))
            row = cur.fetchone()
            if row:
                return row[0]
    reports = sorted(REPORT_DIR.glob(f"*_{locale}.json"))
    for path in reversed(reports):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("gate_passed") and data.get("scope") == "all":
            return data.get("stats")
    return None


def gate(stats: Stats, previous: dict | None) -> list[tuple[str, str]]:
    """Lớp 4: trả về danh sách (mã quy tắc, lý do) — rỗng là đạt."""
    reasons = []
    if stats.files:
        blocked = stats.blocked / stats.files
        if blocked > GATE["max_blocked_ratio"]:
            reasons.append(("gate_blocked_ratio", f"{blocked:.0%} raw bị chặn (ngưỡng {GATE['max_blocked_ratio']:.0%})"))
        rejected = (stats.rejected - stats.blocked) / stats.files
        if rejected > GATE["max_reject_ratio"]:
            reasons.append(("gate_reject_ratio", f"{rejected:.0%} khách sạn bị loại vì lỗi dữ liệu "
                                                 f"(ngưỡng {GATE['max_reject_ratio']:.0%})"))
    if previous:
        for section, rule, limit in (("rooms", "gate_rooms_drop", GATE["max_rooms_drop_pts"]),
                                     ("images", "gate_images_drop", GATE["max_images_drop_pts"])):
            before = (previous.get("pct") or {}).get(section)
            now = stats.pct(section)
            if before is not None and before - now > limit:
                reasons.append((rule, f"tỷ lệ có {section}: {before}% → {now}% (giảm > {limit} điểm)"))
    return reasons


# ------------------------------------------------------------------ báo cáo
def write_report(args, stats: Stats, gate_reasons, all_issues: list, previous, stamp: str, scope: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORT_DIR / f"{stamp}_{args.locale}"
    payload = {
        "locale": args.locale, "currency": args.currency, "mode": "validate_only" if args.validate_only else "load",
        "scope": scope, "parser_version": PARSER_VERSION, "created_at": stamp,
        "gate_passed": not gate_reasons, "gate_reasons": [r for _, r in gate_reasons],
        "stats": stats.as_dict(), "previous_stats": previous,
        "issues": [i.as_dict() for i in all_issues if i.severity in (ERROR, WARNING)],
    }
    base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                                         encoding="utf-8")
    s = stats.as_dict()
    lines = [f"# Kiểm tra dữ liệu v2 — {args.locale}/{args.currency} — {stamp}", "",
             f"- Số file raw: **{s['files']}**",
             f"- Đạt: **{s['hotels_ok']}** · Bị loại cả khách sạn: **{s['hotels_rejected']}** "
             f"(trong đó bị Trip.com chặn: {s['blocked']})",
             f"- Chốt chặn cả đợt: **{'ĐẠT' if not gate_reasons else 'KHÔNG ĐẠT'}**"]
    lines += [f"  - {r}" for _, r in gate_reasons]
    lines += ["", "## Tỷ lệ khách sạn (đạt) có từng mục", "", "| Mục | % |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in s["pct"].items()]
    lines += ["", "## Lỗi và cảnh báo theo quy tắc", "", "| Quy tắc | Mức | Số lần | Ý nghĩa |", "|---|---|---|---|"]
    for rule, count in s["rules"].items():
        layer, severity, text = RULES[rule]
        lines.append(f"| `{rule}` | {severity} (lớp {layer}) | {count} | {text} |")
    lines += ["", "## Tự chuẩn hóa", "", "| Quy tắc | Số lần | Ý nghĩa |", "|---|---|---|"]
    lines += [f"| `{rule}` | {count} | {RULES[rule][2]} |" for rule, count in s["fixed"].items()]
    base.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base.with_suffix(".md")


# ------------------------------------------------------------------ DB
def connect():
    import psycopg2
    return psycopg2.connect(config.dsn())


def save_issues(cur, run_id: int, issues: list) -> None:
    from psycopg2.extras import execute_values
    rows = [(run_id, int(i.trip_hotel_id) if (i.trip_hotel_id or "").isdigit() else None, i.locale, i.layer,
             i.severity, i.rule, i.entity, i.entity_key, i.field, i.value, i.message, i.raw_path)
            for i in issues if i.severity in (ERROR, WARNING)]
    if rows:
        execute_values(cur, """INSERT INTO v2.load_rejects (run_id, trip_hotel_id, locale, layer, severity, rule,
                                   entity, entity_key, field, value, message, raw_path) VALUES %s""",
                       rows, page_size=1000)


# ------------------------------------------------------------------ main
def main(args) -> int:
    args.currency = (args.currency or ("USD" if args.locale.startswith("en") else "VND")).upper()
    files = newest_raw(args.locale, args.currency)
    if not files:
        print(f"Không có raw nào cho {args.locale}/{args.currency}.")
        return 1
    scope = "all"
    if args.ids or args.ids_file:
        wanted = set(args.ids or [])
        if args.ids_file:
            wanted |= {line.strip() for line in Path(args.ids_file).read_text(encoding="utf-8").splitlines()
                       if line.strip() and not line.startswith("#")}
        files = {h: p for h, p in files.items() if h in wanted}
        scope = "subset"
    ordered = sorted(files.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
    if args.limit:
        ordered = ordered[: args.limit]
        scope = "subset"
    other = newest_raw(*OTHER[args.locale]) if not args.no_pair_check else None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"Lượt 1 — kiểm tra {len(ordered)} khách sạn {args.locale}/{args.currency}"
          f"{' (có đối chiếu bản ' + OTHER[args.locale][0] + ')' if other is not None else ''}")
    stats = Stats()
    all_issues: list = []
    accepted: list[tuple[str, Path]] = []
    started = time.time()
    for index, (hotel_id, path) in enumerate(ordered, 1):
        bundle, issues = process(hotel_id, path, args.locale, args.currency, other, not args.no_pair_check)
        stats.add(bundle, issues)
        all_issues.extend(issues.items)
        if bundle is not None:
            accepted.append((hotel_id, path))
        if index % 200 == 0:
            print(f"  {index}/{len(ordered)} · đạt {stats.ok} · loại {stats.rejected} · {time.time() - started:.0f}s",
                  flush=True)

    conn = None
    if not args.validate_only:
        conn = connect()
    previous = previous_stats(conn, args.locale) if scope == "all" else None
    reasons = gate(stats, previous) if scope == "all" else gate(stats, None)
    batch = Issues(None, short_locale(args.locale))
    for rule, reason in reasons:
        batch.add(rule, "batch", detail=reason)
    all_issues.extend(batch.items)
    report = write_report(args, stats, reasons, all_issues, previous, stamp, scope)

    s = stats.as_dict()
    print(f"\nĐạt {s['hotels_ok']} · bị loại {s['hotels_rejected']} (bị chặn {s['blocked']})")
    print("Tỷ lệ có: " + ", ".join(f"{k} {v}%" for k, v in s["pct"].items()))
    top = [(r, c) for r, c in s["rules"].items()][:12]
    if top:
        print("Lỗi/cảnh báo nhiều nhất:")
        for rule, count in top:
            print(f"  {count:6}  {RULES[rule][1]:7}  {rule} — {RULES[rule][2]}")
    print(f"Báo cáo: {report}")
    if reasons:
        print("\n⚠ CHỐT CHẶN CẢ ĐỢT KHÔNG ĐẠT:")
        for _, reason in reasons:
            print(f"  - {reason}")

    if args.validate_only:
        print("\n(--validate-only: không ghi gì vào DB)")
        return 0 if not reasons else 2
    if reasons and not args.force:
        _save_run(conn, args, stats, reasons, all_issues, status="gate_failed")
        print("→ KHÔNG nạp. Xem báo cáo; nếu chắc chắn dữ liệu ổn, chạy lại với --force.")
        return 2

    # ---------------------------------------------------------------- lượt 2: ghi
    from v2.writer import write_bundle
    run_id = _save_run(conn, args, stats, reasons, [], status="running")
    print(f"\nLượt 2 — ghi {len(accepted)} khách sạn vào v2 (run #{run_id})")
    written = failed = 0
    for index, (hotel_id, path) in enumerate(accepted, 1):
        bundle, issues = process(hotel_id, path, args.locale, args.currency, None, False)
        if bundle is None:
            continue
        try:
            with conn.cursor() as cur:
                write_bundle(cur, bundle)
            conn.commit()
            written += 1
        except Exception as exc:
            conn.rollback()
            failed += 1
            bad = Issues(hotel_id, bundle.locale, bundle.raw_path)
            bad.add("db_error", "hotel", value=str(exc).splitlines()[0][:300])
            all_issues.extend(bad.items)
            stats.rule_counts["db_error"] += 1
        if index % 200 == 0:
            print(f"  {index}/{len(accepted)} · ghi {written} · lỗi DB {failed}", flush=True)
    with conn.cursor() as cur:
        save_issues(cur, run_id, all_issues)
        cur.execute("""UPDATE v2.load_runs SET finished_at = now(), status = 'done', hotels_ok = %s,
                           hotels_rejected = %s, stats = %s WHERE id = %s""",
                    (written, stats.rejected + failed, json.dumps(stats.as_dict(), default=str), run_id))
    conn.commit()
    conn.close()
    print(f"Xong: ghi {written}, lỗi DB {failed}. Chi tiết: SELECT * FROM v2.load_rejects WHERE run_id = {run_id};")
    return 0


def _save_run(conn, args, stats: Stats, reasons, issues, status: str) -> int:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO v2.load_runs (locale, currency, mode, status, files_seen, hotels_ok,
                           hotels_rejected, stats, gate_reasons, finished_at)
                       VALUES (%s, %s, 'load', %s, %s, %s, %s, %s, %s,
                               CASE WHEN %s = 'running' THEN NULL ELSE now() END) RETURNING id""",
                    (short_locale(args.locale), args.currency, status, stats.files, stats.ok, stats.rejected,
                     json.dumps(stats.as_dict(), default=str), [r for _, r in reasons] or None, status))
        run_id = cur.fetchone()[0]
        save_issues(cur, run_id, issues)
    conn.commit()
    return run_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--locale", required=True, choices=("vi-VN", "en-US"))
    ap.add_argument("--currency", help="mặc định USD cho en-US, VND cho vi-VN")
    ap.add_argument("--validate-only", action="store_true", help="chỉ kiểm tra và xuất báo cáo, không ghi DB")
    ap.add_argument("--ids", nargs="+", help="chỉ các khách sạn này")
    ap.add_argument("--ids-file", help="file id (mỗi dòng một id), vd output/missing_*.txt")
    ap.add_argument("--limit", type=int, help="chỉ N khách sạn đầu (chạy thử)")
    ap.add_argument("--no-pair-check", action="store_true", help="bỏ đối chiếu với bản thứ tiếng còn lại")
    ap.add_argument("--force", action="store_true", help="vẫn nạp dù chốt chặn cả đợt không đạt")
    sys.exit(main(ap.parse_args()))
