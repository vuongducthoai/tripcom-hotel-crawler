"""Nhận diện Trip.com từ chối — dùng chung cho crawler trình duyệt và HTTP.

Trip.com hay trả HTTP 200 kèm nội dung báo chặn, có khi giấu trong một mảng
byte XOR. Tách riêng ở đây để `crawl_fast.py` dùng được mà không phải import
`crawl_detail.py` (kéo theo Playwright — trái mục đích không mở trình duyệt).
"""
from __future__ import annotations

import json
from typing import Any

ANTIBOT_XOR_KEY = 0x0A


def decode_obfuscated(value: list) -> str | None:
    """Giải mảng byte XOR của Trip.com; None nếu không phải thông báo chặn.

    Dạng này trông như [113, 40, 108, ...] nên bộ dò cũ duyệt qua chỉ thấy
    toàn số nguyên và không nhận ra mình đang bị từ chối.
    """
    if not (50 <= len(value) <= 20_000):
        return None
    if not all(isinstance(byte, int) and 0 <= byte <= 255 for byte in value):
        return None
    text = "".join(chr(byte ^ ANTIBOT_XOR_KEY) for byte in value)
    return text if ("failedcause" in text or "Antibot" in text) else None


def blocked_reason(value: Any) -> str | None:
    """Lý do Trip.com từ chối, None nếu response bình thường.

    Bắt cả hai dạng đã gặp thật:
      - dict có htlSpiderActionErrorCode  (vd 4030)
      - mảng byte XOR có failedcause      (vd Antibot-Gray-ip)
    """
    if isinstance(value, dict):
        if value.get("htlSpiderActionErrorCode") is not None:
            return f"htlSpiderActionErrorCode={value['htlSpiderActionErrorCode']}"
        for child in value.values():
            found = blocked_reason(child)
            if found:
                return found
    elif isinstance(value, list):
        decoded = decode_obfuscated(value)
        if decoded:
            try:
                return str(json.loads(decoded).get("failedcause") or "Antibot")
            except Exception:
                return "Antibot"
        for child in value:
            found = blocked_reason(child)
            if found:
                return found
    return None
