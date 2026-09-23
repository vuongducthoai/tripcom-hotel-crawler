"""Gọi lại các API của trang chi tiết bằng HTTP, không cần trình duyệt.

Trang chi tiết Trip.com render sẵn phần thông tin khách sạn vào HTML, nhưng
giá phòng, địa điểm gần đây và album ảnh thì trang gọi thêm API sau khi tải.
Module này lưu một "mẫu" request bắt được từ lần cào bằng Chromium, rồi thay
mã khách sạn / ngày ở / mã tỉnh-thành để gọi cho các khách sạn khác.

Quy trình:
    1. Cào 1 khách sạn bằng crawl_detail.py (raw sẽ kèm mẫu request).
    2. build_templates(raw) → output/api_templates_<locale>_<currency>.json
    3. crawl_fast.py đọc mẫu đó và gọi lại cho từng khách sạn.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import config
import raw_store

# Chỉ gọi lại những API thật sự cần cho schema v2
REPLAY_APIS = ("getHotelRoomListOversea", "ctGetNearbyPlaceInfo",
               "ctgethotelalbum", "getDetailAdditionalInfo")


def template_path(locale: str, currency: str) -> Path:
    return config.OUTPUT_DIR / f"api_templates_{locale}_{currency.upper()}.json"


# ------------------------------------------------------------------ dựng mẫu
def build_templates(raw_path: Path, locale: str, currency: str) -> tuple[Path, list[str]]:
    """Đọc raw có kèm request → ghi file mẫu. Trả về (đường dẫn, danh sách API)."""
    dump = raw_store.read(raw_path)
    hotel_id = str((dump.get("target") or {}).get("hotel_id")
                   or (dump.get("normalized") or {}).get("trip_hotel_id") or "")
    if not hotel_id:
        raise ValueError(f"Raw không cho biết mã khách sạn: {raw_path}")

    mau: dict[str, Any] = {"hotel_id": hotel_id, "locale": locale,
                           "currency": currency.upper(), "apis": {}}
    for packet in dump.get("responses") or []:
        request = packet.get("request")
        url = packet.get("url") or ""
        if not request or not any(name in url for name in REPLAY_APIS):
            continue
        ten = next(name for name in REPLAY_APIS if name in url)
        mau["apis"][ten] = {
            "url": request.get("url") or url,
            "method": packet.get("method") or "POST",
            "headers": request.get("headers") or {},
            "post_data": request.get("post_data"),
        }

    normalized = dump.get("normalized") or {}
    mau["check_in"] = normalized.get("check_in")
    mau["check_out"] = normalized.get("check_out")
    mau["context"] = _context_from_dump(dump)

    path = template_path(locale, currency)
    path.write_text(json.dumps(mau, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, sorted(mau["apis"])


def _context_from_dump(dump: dict) -> dict:
    """Lấy cityId / provinceId của khách sạn mẫu từ khối hotelDetailResponse."""
    for packet in dump.get("responses") or []:
        if packet.get("url") == "embedded:hotel-detail-response":
            base = (packet.get("response") or {}).get("hotelBaseInfo") or {}
            return {"cityId": base.get("cityId"), "provinceId": base.get("provinceId")}
    return {}


def context_from_detail(detail: dict | None) -> dict:
    base = (detail or {}).get("hotelBaseInfo") or {}
    return {"cityId": base.get("cityId"), "provinceId": base.get("provinceId")}


# --------------------------------------------------------------- thay giá trị
def _compact(date_text: str | None) -> str | None:
    """'2026-10-05' → '20261005' (API phòng dùng dạng này)."""
    return date_text.replace("-", "") if date_text else None


def build_swaps(mau: dict, hotel_id: str, checkin: str, checkout: str,
                context: dict) -> list[tuple[Any, Any]]:
    """Danh sách (giá trị mẫu → giá trị thật) để thay trong payload."""
    cu_id, moi_id = str(mau["hotel_id"]), str(hotel_id)
    swaps: list[tuple[Any, Any]] = [
        (int(cu_id), int(moi_id)), (cu_id, moi_id),
        (mau.get("check_in"), checkin), (mau.get("check_out"), checkout),
        (_compact(mau.get("check_in")), _compact(checkin)),
        (_compact(mau.get("check_out")), _compact(checkout)),
    ]
    cu_ctx = mau.get("context") or {}
    for khoa in ("cityId", "provinceId"):
        cu, moi = cu_ctx.get(khoa), context.get(khoa)
        if cu is not None and moi is not None and cu != moi:
            swaps.append((cu, moi))
    return [(a, b) for a, b in swaps if a is not None and b is not None and a != b]


def apply_swaps(value: Any, swaps: list[tuple[Any, Any]]) -> Any:
    """Duyệt đệ quy payload, thay đúng những giá trị của khách sạn mẫu."""
    if isinstance(value, dict):
        return {k: apply_swaps(v, swaps) for k, v in value.items()}
    if isinstance(value, list):
        return [apply_swaps(v, swaps) for v in value]
    for cu, moi in swaps:
        if type(value) is type(cu) and value == cu:
            return moi
    return value


def payload_for(mau: dict, ten_api: str, hotel_id: str, checkin: str,
                checkout: str, context: dict) -> tuple[str, dict, dict]:
    """Trả về (url, headers, body) đã thay số liệu cho khách sạn cần cào."""
    api = mau["apis"][ten_api]
    swaps = build_swaps(mau, hotel_id, checkin, checkout, context)
    body = json.loads(api["post_data"]) if api.get("post_data") else {}
    url = api["url"]
    for cu, moi in swaps:
        if isinstance(cu, str):
            url = url.replace(cu, str(moi))
    return url, dict(api.get("headers") or {}), apply_swaps(copy.deepcopy(body), swaps)


def load_templates(locale: str, currency: str) -> dict | None:
    path = template_path(locale, currency)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
