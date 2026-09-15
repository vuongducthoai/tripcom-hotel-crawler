"""Trích dữ liệu từ HTML bằng CSS selector — không dùng LLM.

Cố ý không dùng LLMExtractionStrategy: với vài nghìn khách sạn thì tốn tiền
vô lý và kết quả không ổn định giữa các lần chạy. CSS selector chạy miễn phí,
deterministic, và khi Trip.com đổi layout thì lỗi hiện ra ngay chứ không âm thầm
trả về dữ liệu sai.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE = "https://vn.trip.com"

_PRICE_RE = re.compile(r"[\d.,]+")


def _field(node, spec: dict) -> str | None:
    sel = spec["selector"]
    target = node if sel == "self" else node.select_one(sel)
    if target is None:
        return None

    kind = spec.get("type", "text")
    if kind == "text":
        return " ".join(target.get_text(" ", strip=True).split()) or None
    if kind == "html":
        return str(target)
    if kind.startswith("attr:"):
        val = target.get(kind.split(":", 1)[1])
        if isinstance(val, list):
            val = " ".join(val)
        return val or None
    return None


def parse_price(raw: str | None) -> float | None:
    """'₫ 1.234.567' → 1234567.0. Trả None nếu không parse được."""
    if not raw:
        return None
    m = _PRICE_RE.search(raw.replace("\xa0", " "))
    if not m:
        return None
    token = m.group(0)
    # VND dùng dấu chấm ngăn nghìn; bỏ hết dấu phân cách.
    token = token.replace(".", "").replace(",", "")
    try:
        return float(token)
    except ValueError:
        return None


def trip_hotel_id(url: str | None) -> str | None:
    """Rút id khách sạn từ URL chi tiết, làm natural key để upsert.

    Phải gọi trên URL CÒN NGUYÊN query string — id thường nằm ở ?hotelId=.
    """
    if not url:
        return None
    m = re.search(r"[?&]hotel(?:Id|_id)=(\d+)", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"/hotels?/(?:detail|[a-z-]+)[^?]*?(\d{5,})", url)
    return m.group(1) if m else None


def clean_url(url: str) -> str:
    """Bỏ tham số theo dõi/ngày tháng, giữ lại hotelId để URL còn mở được."""
    full = urljoin(BASE, url)
    base, _, query = full.partition("?")
    keep = [
        p for p in query.split("&")
        if p.split("=")[0].lower() in ("hotelid", "hotel_id", "cityid")
    ]
    return f"{base}?{'&'.join(keep)}" if keep else base


def extract(html: str, schema: dict) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    for node in soup.select(schema["baseSelector"]):
        row = {f["name"]: _field(node, f) for f in schema["fields"]}
        if not any(row.values()):
            continue
        # Rút id TRƯỚC khi dọn URL, vì id nằm trong query string.
        row["trip_hotel_id"] = trip_hotel_id(row.get("url"))
        if row.get("url"):
            row["url"] = clean_url(row["url"])
        row["price_value"] = parse_price(row.get("price"))
        rows.append(row)
    return rows


def dedupe(rows: list[dict]) -> list[dict]:
    """Bỏ trùng theo trip_hotel_id, giữ bản ghi đầy đủ field nhất."""
    best: dict[str, dict] = {}
    extras: list[dict] = []
    for r in rows:
        key = r.get("trip_hotel_id")
        if not key:
            extras.append(r)
            continue
        filled = sum(1 for v in r.values() if v)
        if key not in best or filled > sum(1 for v in best[key].values() if v):
            best[key] = r
    return list(best.values()) + extras


def probe(html: str, candidates: list[dict]) -> list[tuple[str, int, int, dict]]:
    """Thử từng bộ selector, trả (tên, số card, số card có tên+url, mẫu đầu)."""
    out = []
    for schema in candidates:
        try:
            rows = extract(html, schema)
        except Exception:
            rows = []
        good = sum(1 for r in rows if r.get("name") and r.get("url"))
        out.append((schema["name"], len(rows), good, rows[0] if rows else {}))
    return sorted(out, key=lambda t: (t[2], t[1]), reverse=True)
