"""Content semantic validation for Trip.com responses.

Rejects 200 OK responses that conceal WAF blocks, XOR obfuscation,
or empty shells, ensuring requests are retried instead of yielding empty records.
"""
from __future__ import annotations

import json
import re
from typing import Any

ANTIBOT_XOR_KEY = 0x0A

CHALLENGE_KEYWORDS = (
    "cf-browser-verification",
    "cf-challenge",
    "challenge-running",
    "geetest",
    "waf-block",
    "security verification",
    "robot check",
    "verify you are human",
    "access denied",
    "blocked by bot detection",
)


def decode_obfuscated(value: list[int] | Any) -> str | None:
    """Decode Trip.com XOR 0x0A obfuscated byte arrays."""
    if not isinstance(value, list) or not (50 <= len(value) <= 25_000):
        return None
    if not all(isinstance(b, int) and 0 <= b <= 255 for b in value[:100]):
        return None
    try:
        text = "".join(chr(b ^ ANTIBOT_XOR_KEY) for b in value)
        if "failedcause" in text or "Antibot" in text or "htlSpiderAction" in text:
            return text
    except Exception:
        pass
    return None


def detect_blocked_reason(value: Any) -> str | None:
    """Traverse JSON structure to detect hidden Trip.com block markers."""
    if isinstance(value, dict):
        if value.get("htlSpiderActionErrorCode") is not None:
            return f"htlSpiderActionErrorCode={value['htlSpiderActionErrorCode']}"
        if "failedcause" in value:
            return f"failedcause={value['failedcause']}"
        for v in value.values():
            found = detect_blocked_reason(v)
            if found:
                return found
    elif isinstance(value, list):
        decoded = decode_obfuscated(value)
        if decoded:
            try:
                parsed = json.loads(decoded)
                return str(parsed.get("failedcause") or "Antibot-Gray-ip")
            except Exception:
                return "Antibot-Obfuscated"
        for item in value:
            found = detect_blocked_reason(item)
            if found:
                return found
    return None


def validate_hotel_html(html_text: str, expected_hotel_id: str | int | None = None) -> tuple[bool, str | None]:
    """Validate that HTML content is a genuine hotel page and not a block or empty shell.

    Returns:
        (is_valid, error_reason)
    """
    if not html_text:
        return False, "Response body is empty"

    lower = html_text.casefold()

    # 1. Check for challenge / CAPTCHA markers
    for kw in CHALLENGE_KEYWORDS:
        if kw in lower:
            return False, f"Anti-bot challenge detected: {kw}"

    # 2. Check for XOR obfuscated JSON embedded in HTML
    trimmed = html_text.strip()
    if trimmed.startswith("[") and trimmed.endswith("]"):
        try:
            raw_list = json.loads(trimmed)
            if isinstance(raw_list, list):
                reason = detect_blocked_reason(raw_list)
                if reason:
                    return False, f"Trip.com blocked: {reason}"
        except Exception:
            pass

    # 3. Check for specific Trip.com error strings
    if "antibot-gray-ip" in lower:
        return False, "Trip.com blocked: Antibot-Gray-ip"
    if "htlspideractionerrorcode" in lower:
        match = re.search(r"htlSpiderActionErrorCode[\"']?\s*[:=]\s*(\d+)", html_text, re.I)
        code = match.group(1) if match else "unknown"
        return False, f"Trip.com blocked: htlSpiderActionErrorCode={code}"

    # 4. Length check after explicit block keywords
    if len(trimmed) < 400:
        return False, "Response body is too short (<400 bytes)"

    # 5. Check for presence of real hotel SSR data
    has_next_f = "__next_f.push" in html_text
    has_json_ld = "application/ld+json" in html_text

    if not (has_next_f or has_json_ld):
        if "<title>" in lower and ("not found" in lower or "error" in lower):
            return False, "Trip.com page error or not found"
        return False, "Missing SSR payload (__next_f.push and application/ld+json absent)"

    return True, None
