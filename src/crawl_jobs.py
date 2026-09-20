"""Chạy các script crawl từ giao diện web, thay cho gõ lệnh tay.

Nguyên tắc an toàn:
  - CHỈ chạy được các job có sẵn trong JOB_SPECS. Không có đường nào để web
    truyền lệnh tuỳ ý xuống shell.
  - Mọi tham số đều được kiểm và ép kiểu trước khi dựng argv.
  - Gọi subprocess bằng danh sách argv, KHÔNG dùng shell=True.
  - Mỗi lần chỉ một job chạy: crawl_api và crawl_detail dùng chung
    browser_profile, chạy song song là hỏng profile.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import config

MAX_LOG_LINES = 400

# Tiến độ in ra theo dạng "[2617/3431] ..." hoặc "Tiến độ: 750/1491 raw (50%)"
PROGRESS_PATTERNS = (
    re.compile(r"\[(\d+)\s*/\s*(\d+)[^\]]*\]"),
    re.compile(r"Tiến độ:\s*(\d+)\s*/\s*(\d+)"),
)


def _int_arg(value: Any, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} phải là số nguyên.") from None
    if not low <= number <= high:
        raise ValueError(f"{name} phải nằm trong khoảng {low}–{high}.")
    return number


def _choice_arg(value: Any, name: str, allowed: set[str], default: str) -> str:
    text = str(value or default)
    if text not in allowed:
        raise ValueError(f"{name} chỉ nhận: {', '.join(sorted(allowed))}.")
    return text


LOCALES = {"vi-VN", "en-US"}
CURRENCIES = {"VND", "USD"}


def _market(params: dict) -> list[str]:
    locale = _choice_arg(params.get("locale"), "locale", LOCALES, "vi-VN")
    currency = _choice_arg(params.get("currency"), "currency", CURRENCIES,
                           "VND" if locale == "vi-VN" else "USD")
    return ["--locale", locale, "--currency", currency]


def _build_overview(params: dict) -> list[str]:
    argv = ["src/crawl_api.py", "--city-id",
            str(_int_arg(params.get("city_id", 301), "city_id", 1, 999_999))]
    if params.get("max_pages"):
        argv += ["--max-pages", str(_int_arg(params["max_pages"], "max_pages", 1, 400))]
    return argv


def _build_detail(params: dict) -> list[str]:
    argv = ["src/crawl_detail.py", "--from-db"] + _market(params)
    argv += ["--workers", str(_int_arg(params.get("workers", 2), "workers", 1, 3))]
    argv += ["--max-consecutive-errors",
             str(_int_arg(params.get("max_errors", 5), "max_errors", 1, 50))]
    if params.get("limit"):
        argv += ["--limit", str(_int_arg(params["limit"], "limit", 1, 100_000))]
    if params.get("missing_only"):
        argv += ["--missing-only"]
    if params.get("profile_dir"):
        # Chỉ cho tên thư mục đơn giản, không cho đường dẫn tuỳ ý.
        name = str(params["profile_dir"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
            raise ValueError("profile_dir không hợp lệ.")
        argv += ["--profile-dir", name]
    return argv


def _build_reparse(params: dict) -> list[str]:
    return ["scripts/reparse_details.py"] + _market(params)


def _build_loader(params: dict) -> list[str]:
    argv = ["src/db/detail_loader.py"]
    if params.get("no_prices"):
        argv += ["--no-prices"]
    return argv


def _build_amenities(params: dict) -> list[str]:
    locale = _choice_arg(params.get("locale"), "locale", LOCALES, "vi-VN")
    argv = ["scripts/repair_hotel_amenities.py", "--locale", locale,
            "--city-id", str(_int_arg(params.get("city_id", 301), "city_id", 1, 999_999)),
            "--workers", str(_int_arg(params.get("workers", 2), "workers", 1, 3))]
    if params.get("apply"):
        argv += ["--apply"]
    return argv


def _build_audit(params: dict) -> list[str]:
    return ["scripts/audit_data.py"]


JOB_SPECS: dict[str, dict[str, Any]] = {
    "overview": {"label": "Cào danh sách khách sạn", "build": _build_overview,
                 "note": "Gọi Trip.com — chạy chậm, đừng cùng lúc với job khác."},
    "detail": {"label": "Cào chi tiết khách sạn", "build": _build_detail,
               "note": "Gọi Trip.com. Tự dừng khi bị chặn liên tiếp."},
    "reparse": {"label": "Tái phân tích raw", "build": _build_reparse,
                "note": "Không gọi mạng, chỉ đọc lại file đã cào."},
    "loader": {"label": "Nạp vào PostgreSQL", "build": _build_loader,
               "note": "Không gọi mạng. Nạp file manifest mới nhất."},
    "amenities": {"label": "Sửa tiện nghi khách sạn", "build": _build_amenities,
                  "note": "Gọi Trip.com."},
    "audit": {"label": "Kiểm tra dữ liệu", "build": _build_audit,
              "note": "Chỉ đọc DB, an toàn."},
}


class Job:
    def __init__(self, key: str, argv: list[str]) -> None:
        self.key = key
        self.argv = argv
        self.started_at = datetime.now()
        self.finished_at: datetime | None = None
        self.returncode: int | None = None
        self.done = 0
        self.total = 0
        self.lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self.process: subprocess.Popen | None = None
        self._stopping = False

    def snapshot(self) -> dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        return {
            "key": self.key,
            "label": JOB_SPECS[self.key]["label"],
            "command": " ".join(self.argv),
            "running": running,
            "stopping": self._stopping,
            "returncode": self.returncode,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "done": self.done,
            "total": self.total,
            "percent": round(100 * self.done / self.total, 1) if self.total else None,
            "lines": list(self.lines),
        }


class JobRunner:
    """Giữ đúng một job tại một thời điểm."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Job | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=20)

    # ------------------------------------------------------------- truy vấn
    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._current.snapshot() if self._current else None
            history = list(self._history)
        return {"current": current, "history": history,
                "jobs": [{"key": key, "label": spec["label"], "note": spec["note"]}
                         for key, spec in JOB_SPECS.items()]}

    def is_running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.process is not None \
                and self._current.process.poll() is None

    # ------------------------------------------------------------- điều khiển
    def start(self, key: str, params: dict) -> dict[str, Any]:
        if key not in JOB_SPECS:
            raise ValueError(f"Job không hợp lệ: {key}")
        if self.is_running():
            raise ValueError(
                "Đang có job chạy. Mỗi lần chỉ chạy một job vì các script dùng "
                "chung browser profile."
            )
        script_args = JOB_SPECS[key]["build"](params)
        script = config.ROOT / script_args[0]
        if not script.is_file():
            raise ValueError(f"Không tìm thấy script: {script_args[0]}")
        argv = [sys.executable, str(script), *script_args[1:]]

        job = Job(key, [Path(argv[1]).name, *argv[2:]])
        job.process = subprocess.Popen(
            argv, cwd=str(config.ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        with self._lock:
            self._current = job
        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            job = self._current
        if not job or not job.process or job.process.poll() is not None:
            raise ValueError("Không có job nào đang chạy.")
        job._stopping = True
        job.lines.append("[đã bấm dừng — chờ tiến trình thoát]")
        job.process.terminate()
        return job.snapshot()

    # ------------------------------------------------------------- nội bộ
    def _pump(self, job: Job) -> None:
        assert job.process and job.process.stdout
        for line in job.process.stdout:
            text = line.rstrip()
            if not text:
                continue
            job.lines.append(text)
            for pattern in PROGRESS_PATTERNS:
                found = pattern.search(text)
                if found:
                    job.done, job.total = int(found.group(1)), int(found.group(2))
                    break
        job.process.wait()
        job.returncode = job.process.returncode
        job.finished_at = datetime.now()
        with self._lock:
            self._history.appendleft(job.snapshot())


RUNNER = JobRunner()
