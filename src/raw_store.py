"""Đọc/ghi raw capture của trang chi tiết, có nén gzip.

Raw là JSON thuần nên nén được khoảng 10 lần — với quy mô nhiều tỉnh thì đây là
khác biệt giữa vài GB và vài chục GB.

Quy ước: **ghi ra `.json.gz`, nhưng đọc được cả `.json` cũ.** Nhờ vậy toàn bộ
raw đã cào trước đó vẫn dùng được, không phải cào lại. Mọi nơi trong dự án
truyền vào đường dẫn `.json` như cũ, module này tự tìm bản có thật.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Iterator

GZ_SUFFIX = ".json.gz"
PLAIN_SUFFIX = ".json"


def gz_path(path: Path) -> Path:
    """`.../123.json` → `.../123.json.gz`"""
    return path if path.name.endswith(GZ_SUFFIX) else path.with_name(path.name + ".gz")


def plain_path(path: Path) -> Path:
    """`.../123.json.gz` → `.../123.json`"""
    return path.with_name(path.name[: -len(".gz")]) if path.name.endswith(GZ_SUFFIX) else path


def hotel_id(path: Path) -> str:
    """Lấy mã khách sạn từ tên file, đúng cho cả `.json` lẫn `.json.gz`.

    Không dùng `path.stem` được: với `123.json.gz` nó trả về `123.json`.
    """
    name = path.name
    for suffix in (GZ_SUFFIX, PLAIN_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def resolve(path: Path) -> Path | None:
    """File thật đang nằm ở đâu: ưu tiên bản nén, rồi tới bản cũ. None nếu chưa có."""
    compressed = gz_path(path)
    if compressed.exists():
        return compressed
    plain = plain_path(path)
    return plain if plain.exists() else None


def exists(path: Path) -> bool:
    return resolve(path) is not None


def read(path: Path) -> dict:
    """Đọc raw dù đang ở dạng nén hay không."""
    found = resolve(path)
    if found is None:
        raise FileNotFoundError(path)
    if found.name.endswith(GZ_SUFFIX):
        with gzip.open(found, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(found.read_text(encoding="utf-8"))


def write(path: Path, payload: dict, *, attempts: int = 6) -> Path:
    """Ghi raw ra `.json.gz`; xoá bản `.json` cũ sau khi bản nén đã ghi xong.

    Ghi vào file tạm rồi mới đổi tên, để lỡ tắt máy giữa chừng thì không còn
    lại file hỏng. Trên Windows antivirus hay giữ file vừa ghi một lúc nên
    `replace` được thử lại vài lần trước khi chịu thua.
    """
    target = gz_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    for attempt in range(attempts):
        try:
            temporary.replace(target)
            break
        except PermissionError:
            if attempt == attempts - 1:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.2 * (attempt + 1))
    stale = plain_path(path)
    if stale.exists():
        try:
            stale.unlink()
        except OSError:
            pass  # Xoá không được thì thôi, bản nén mới là bản được đọc.
    return target


def iter_raw_files(directory: Path) -> Iterator[Path]:
    """Liệt kê raw trong thư mục — mỗi khách sạn đúng một file, ưu tiên bản nén."""
    if not directory.exists():
        return
    compressed = sorted(directory.glob("*" + GZ_SUFFIX))
    seen = {hotel_id(path) for path in compressed}
    yield from compressed
    for path in sorted(directory.glob("*" + PLAIN_SUFFIX)):
        if hotel_id(path) not in seen:
            yield path
