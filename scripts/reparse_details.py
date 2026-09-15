"""Tái phân tích output/details/raw/*.json bằng detail_extract.py hiện tại,
KHÔNG crawl lại mạng. Dùng khi sửa logic extract sau khi đã crawl xong.

    python scripts/reparse_details.py

Ghi ra output/data/hotel_details_reparsed_<timestamp>.json, đúng format mà
db/detail_loader.py đang đọc (source_overview/crawled_at/complete/count/
success_count/details[]).
"""
from __future__ import annotations

import json
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from detail_extract import extract_detail  # noqa: E402
import config  # noqa: E402


def main(args: argparse.Namespace) -> None:
    raw_dir = config.OUTPUT_DIR / "details" / "raw"
    raw_files = sorted(raw_dir.glob("*.json"))
    if not raw_files:
        raise SystemExit(f"Không tìm thấy raw capture nào trong {raw_dir}")

    details: list[dict] = []
    ok = 0
    changed_rooms = 0

    for path in raw_files:
        try:
            dump = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  BỎ QUA {path.name}: không đọc được ({exc})")
            continue

        target = dump.get("target") or {}
        base = dict(dump.get("normalized") or {})
        hotel_id = str(target.get("trip_hotel_id") or base.get("trip_hotel_id") or path.stem)
        url = target.get("url") or base.get("url") or ""
        responses = dump.get("responses") or []

        old_room_count = len(base.get("rooms") or [])
        try:
            fresh = extract_detail(responses, hotel_id, url)
        except Exception as exc:
            print(f"  LỖI extract {path.name}: {type(exc).__name__}: {exc}")
            base.setdefault("trip_hotel_id", hotel_id)
            base.setdefault("url", url)
            base["success"] = False
            base["error"] = f"reparse_failed: {exc}"
            details.append(base)
            continue

        base["trip_hotel_id"] = hotel_id
        base["url"] = url
        base["description"] = fresh["description"] or base.get("description")
        base["hotel_type"] = fresh["hotel_type"] or base.get("hotel_type")
        base["images"] = fresh["images"]
        base["amenities"] = fresh["amenities"]
        base["rooms"] = fresh["rooms"]
        base["response_count"] = fresh["response_count"]
        base["parser_version"] = fresh["parser_version"]
        base.setdefault("success", True)
        base.setdefault("error", None)
        base.setdefault("check_in", None)
        base.setdefault("check_out", None)

        if len(fresh["rooms"]) != old_room_count:
            changed_rooms += 1
        ok += 1
        details.append(base)
        if not args.no_update_cache:
            dump["normalized"] = base
            path.write_text(json.dumps(dump, ensure_ascii=False), encoding="utf-8")

    out = {
        "source_overview": "reparse_from_raw_cache",
        "crawled_at": datetime.now().isoformat(),
        "complete": True,
        "count": len(details),
        "success_count": ok,
        "details": details,
    }

    out_path = config.DATA_DIR / f"hotel_details_reparsed_{datetime.now():%Y%m%d_%H%M%S}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    print(f"Đã tái phân tích {len(raw_files)} file raw -> {ok} thành công.")
    print(f"Số khách sạn có số phòng thay đổi so với lần chạy trước: {changed_rooms}")
    print(f"Ghi ra: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-update-cache", action="store_true",
        help="chỉ tạo manifest, không cập nhật normalized trong raw cache",
    )
    main(parser.parse_args())
