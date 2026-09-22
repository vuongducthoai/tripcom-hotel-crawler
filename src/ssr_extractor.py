"""Extract structured hotel details directly from raw HTML and Next.js SSR streams.

Decoupled completely from browser/Playwright DOM APIs.
Parses:
  - JSON-LD (<script type="application/ld+json">)
  - Next.js Server Components flight chunks (self.__next_f.push)
  - Hotel Description (hotelDescriptionInfo)
  - Hotel Facilities (hotelFacilityPopV2)
  - Meta tags and inline policy blocks
Converts everything into canonical synthetic packets and delegates to detail_extract.py.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any

from bs4 import BeautifulSoup

from api_extract import _next_f_text
from detail_extract import extract_detail
from hotel_description import description_from_scripts
from hotel_facilities import normalize_facility_payload, payload_from_scripts

DETAIL_BLOCK_URL = "embedded:hotel-detail-response"
_JSON = json.JSONDecoder()

PUSH_RE = re.compile(r"(?:self|window)\.__next_f\.push\(\s*")
JSON_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)
SCRIPT_RE = re.compile(r'<script\b[^>]*>(.*?)</script>', re.I | re.S)
META_DESC_RE = re.compile(r'<meta[^>]+(?:name=["\']description["\']|property=["\']og:description["\'])[^>]+content=["\']([^"\']*)["\']', re.I)
META_DESC_RE_REV = re.compile(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:name=["\']description["\']|property=["\']og:description["\'])', re.I)


def extract_script_texts(html_text: str) -> list[str]:
    """Extract inner text of all <script> tags."""
    return SCRIPT_RE.findall(html_text)


def extract_json_ld(html_text: str) -> list[dict]:
    """Extract and parse all JSON-LD blocks."""
    results = []
    for match in JSON_LD_RE.findall(html_text):
        content = match.strip()
        if not content:
            continue
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                results.extend([item for item in parsed if isinstance(item, dict)])
            elif isinstance(parsed, dict):
                results.append(parsed)
        except Exception:
            continue
    return results


def extract_meta_description(html_text: str) -> str | None:
    """Extract page meta description content."""
    m = META_DESC_RE.search(html_text) or META_DESC_RE_REV.search(html_text)
    if m:
        val = html.unescape(m.group(1).strip())
        if val:
            return val
    return None


def extract_next_flight_chunks(scripts: list[str]) -> list[Any]:
    """Decode all Next.js Server Components flight chunks (__next_f.push)."""
    decoder = json.JSONDecoder()
    chunks: list[Any] = []
    for script in scripts:
        if "__next_f.push" not in script:
            continue
        for match in PUSH_RE.finditer(script):
            try:
                arg, _ = decoder.raw_decode(script[match.end():])
            except ValueError:
                continue

            # In Next.js flight format, arg is usually [type, chunk_text_or_object]
            if isinstance(arg, list) and len(arg) >= 2:
                payload = arg[1]
                if isinstance(payload, str):
                    # Check if string contains JSON lines (e.g. "1:HL[...]\n2:I{...}\n")
                    for line in payload.splitlines():
                        line = line.strip()
                        if not line or ":" not in line:
                            continue
                        prefix, rest = line.split(":", 1)
                        if rest.startswith(("{", "[")):
                            try:
                                chunks.append(json.loads(rest))
                            except ValueError:
                                pass
                elif isinstance(payload, (dict, list)):
                    chunks.append(payload)
            elif isinstance(arg, (dict, list)):
                chunks.append(arg)

    return chunks


def extract_hotel_detail_response(
    html_text: str, flight_chunks: list[Any] | None = None
) -> dict | None:
    """Extract embedded hotelDetailResponse payload from Next.js SSR stream.

    Ensures 100% parity with crawl_detail._capture_detail_response for Schema V2 compatibility.
    """
    # 1. Primary: regex-decoded next_f text stream (fast & identical to crawl_detail)
    try:
        text = _next_f_text(html_text)
        if text:
            anchor = '"hotelDetailResponse":'
            index = text.find(anchor)
            if index >= 0:
                value, _ = _JSON.raw_decode(text, index + len(anchor))
                if isinstance(value, dict) and value.get("hotelBaseInfo"):
                    return value
    except Exception:
        pass

    # 2. Fallback: search across parsed flight chunks if available
    if flight_chunks:
        def _find_detail(obj: Any) -> dict | None:
            if isinstance(obj, dict):
                if "hotelDetailResponse" in obj and isinstance(obj["hotelDetailResponse"], dict):
                    res = obj["hotelDetailResponse"]
                    if res.get("hotelBaseInfo"):
                        return res
                if "hotelBaseInfo" in obj and ("hotelPolicyInfo" in obj or "starInfo" in obj):
                    return obj
                for v in obj.values():
                    found = _find_detail(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for item in obj:
                    found = _find_detail(item)
                    if found:
                        return found
            return None

        for chunk in flight_chunks:
            found = _find_detail(chunk)
            if found:
                return found

    return None


def parse_hotel_html(
    html_text: str,
    hotel_id: str | int,
    url: str,
    currency: str = "VND",
    locale: str = "vi-VN",
    additional_packets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Parse raw HTML string and return normalized hotel details dictionary."""
    hotel_id_str = str(hotel_id)
    scripts = extract_script_texts(html_text)
    packets: list[dict[str, Any]] = []

    # 1. JSON-LD scripts
    for node in extract_json_ld(html_text):
        packets.append({
            "url": "embedded:json-ld",
            "method": "EMBEDDED",
            "status": 200,
            "response": node,
        })

    # 1b. Fallback for Name and Address from SSR HTML DOM (h1 and addressText classes)
    soup = BeautifulSoup(html_text, "html.parser")
    h1 = soup.find("h1")
    dom_name = h1.text.strip() if h1 and h1.text else None

    addr_el = soup.find(class_=re.compile(r"addressText|addressDesc", re.I))
    dom_address = addr_el.text.strip() if addr_el and addr_el.text else None

    if dom_name or dom_address:
        packets.append({
            "url": "embedded:json-ld",
            "method": "EMBEDDED",
            "status": 200,
            "response": {
                "@type": "Hotel",
                "name": dom_name,
                "address": dom_address,
            },
        })

    # 2. Hotel Description from scripts
    desc_info = description_from_scripts(scripts, hotel_id_str)
    if desc_info:
        packets.append({
            "url": "embedded:hotel-description",
            "method": "EMBEDDED",
            "status": 200,
            "response": {
                "hotel_id": hotel_id_str,
                "hotelDescriptionInfo": desc_info,
            },
        })

    # 3. Hotel Facilities from scripts
    facility_raw = payload_from_scripts(scripts, hotel_id_str)
    if facility_raw:
        normalized_fac = normalize_facility_payload(facility_raw)
        if normalized_fac:
            packets.append({
                "url": "embedded:hotel-facilities",
                "method": "EMBEDDED",
                "status": 200,
                "response": normalized_fac,
            })

    # 4. Meta description
    meta_desc = extract_meta_description(html_text)
    if meta_desc:
        packets.append({
            "url": "embedded:page-meta",
            "method": "EMBEDDED",
            "status": 200,
            "response": {"description": meta_desc},
        })

    # 5. Next.js flight data chunks (images, room maps, nearby POIs, policies)
    def extract_nested_room_payloads(obj: Any) -> list[dict]:
        found = []
        if isinstance(obj, dict):
            if "physicRoomMap" in obj or "roomPopInfo" in obj:
                found.append(obj)
            for val in obj.values():
                found.extend(extract_nested_room_payloads(val))
        elif isinstance(obj, list):
            for item in obj:
                found.extend(extract_nested_room_payloads(item))
        return found

    flight_chunks = extract_next_flight_chunks(scripts)
    for chunk in flight_chunks:
        packets.append({
            "url": "embedded:next-f-chunk",
            "method": "EMBEDDED",
            "status": 200,
            "response": chunk,
        })
        for room_obj in extract_nested_room_payloads(chunk):
            packets.append({
                "url": "embedded:hotel-rooms",
                "method": "EMBEDDED",
                "status": 200,
                "response": room_obj,
            })

    # 5b. Schema V2 hotelDetailResponse block
    detail_block = extract_hotel_detail_response(html_text, flight_chunks)
    if detail_block:
        packets.append({
            "url": DETAIL_BLOCK_URL,
            "method": "EMBEDDED",
            "status": 200,
            "response": detail_block,
        })

    # 6. Any additional packets (e.g. dynamic room list API call if enabled)
    if additional_packets:
        packets.extend(additional_packets)

    # 7. Extract normalized record via canonical parser
    normalized = extract_detail(packets, hotel_id_str, url, currency, locale)
    normalized["has_detail_block"] = bool(detail_block)

    # Attach packets metadata for raw_store compatibility
    return {
        "normalized": normalized,
        "packets": packets,
    }
