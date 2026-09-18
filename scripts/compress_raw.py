"""Nén các file raw .json đã cào thành .json.gz để lấy lại dung lượng đĩa.

    python scripts/compress_raw.py --dry-run     # chỉ xem sẽ tiết kiệm bao nhiêu
    python scripts/compress_raw.py               # nén thật

An toàn: với mỗi file, ghi bản nén ra trước, ĐỌC LẠI và đối chiếu bằng đúng
nội dung gốc, khớp rồi mới xoá bản .json. Chạy lại được nhiều lần.

KHÔNG chạy khi đang có crawl chạy — file đang ghi dở sẽ bị đọc nhầm.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

import config  # noqa: E402
import raw_store  # noqa: E402


def mb(value: int) -> str:
    return f"{value / 1024 / 1024:,.0f} MB"


def main(args: argparse.Namespace) -> None:
    root = Path(args.dir) if args.dir else config.OUTPUT_DIR / "details" / "raw"
    if not root.exists():
        raise SystemExit(f"Không thấy thư mục {root}")

    plain_files = sorted(root.rglob("*.json"))
    if not plain_files:
        print(f"Không còn file .json nào trong {root} — có thể đã nén hết rồi.")
        return

    before = after = 0
    done = skipped = failed = 0

    for index, path in enumerate(plain_files, 1):
        size = path.stat().st_size
        before += size
        target = raw_store.gz_path(path)

        try:
            original = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  BỎ QUA {path.name}: đọc không được ({exc})")
            failed += 1
            after += size
            continue

        if args.dry_run:
            after += size // 10  # JSON nén gzip thường còn khoảng 1/10
            done += 1
        else:
            try:
                raw_store.write(path, original)
                # Đọc lại từ chính bản nén rồi so với bản gốc: khớp mới yên tâm.
                if raw_store.read(target) != original:
                    raise ValueError("nội dung bản nén không khớp bản gốc")
            except Exception as exc:
                print(f"  LỖI {path.name}: {exc} — giữ nguyên bản .json")
                failed += 1
                after += size
                continue
            after += target.stat().st_size
            done += 1

        if index % 200 == 0 or index == len(plain_files):
            print(f"  {index}/{len(plain_files)} file "
                  f"({index * 100 // len(plain_files)}%)", flush=True)

    print()
    print(f"Xử lý {len(plain_files)} file: {done} nén, {failed} lỗi, {skipped} bỏ qua")
    print(f"Trước: {mb(before)}  →  Sau: {mb(after)}  (tiết kiệm {mb(before - after)})")
    if args.dry_run:
        print("Đây là --dry-run, chưa đụng vào file nào. Bỏ cờ đó để nén thật.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", help="thư mục raw; mặc định output/details/raw")
    parser.add_argument("--dry-run", action="store_true",
                        help="chỉ ước lượng dung lượng tiết kiệm, không ghi/xoá gì")
    main(parser.parse_args())
