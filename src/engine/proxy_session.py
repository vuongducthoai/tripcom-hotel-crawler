"""Proxy session management for rotating residential, multi-port, and proxy list files."""
from __future__ import annotations

import hashlib
import os
import random
import re
import string
from pathlib import Path
from typing import Any

PORT_RANGE_RE = re.compile(r"\{port:(\d+)-(\d+)\}")


def normalize_proxy_url(raw: str) -> str:
    """Convert standard host:port:user:pass or host:port into standard URL format."""
    text = raw.strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "socks5://")):
        return text

    parts = text.split(":")
    if len(parts) == 4:
        # Format: host:port:username:password
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    elif len(parts) == 2:
        # Format: host:port
        host, port = parts
        return f"http://{host}:{port}"
    return text


def load_proxy_pool(source: str | None = None) -> list[str]:
    """Load list of proxies from string, comma-separated list, or file path."""
    val = (source or os.getenv("PROXY_URL") or "").strip()
    if not val:
        return []

    # Check if value points to an existing text file (e.g. proxies.txt)
    p = Path(val)
    if p.exists() and p.is_file():
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
            return [normalize_proxy_url(line) for line in lines if line.strip() and not line.strip().startswith("#")]
        except Exception:
            pass

    # Check if multiple lines or comma-separated
    if "\n" in val:
        return [normalize_proxy_url(line) for line in val.splitlines() if line.strip()]
    if "," in val:
        return [normalize_proxy_url(item) for item in val.split(",") if item.strip()]

    return [normalize_proxy_url(val)]


def generate_session_id(hotel_id: str | int | None = None, attempt: int = 1) -> str:
    """Generate a unique random session identifier for sticky proxy rotation."""
    rand_suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    if hotel_id:
        return f"sess_{hotel_id}_{attempt}_{rand_suffix}"
    return f"sess_{rand_suffix}"


def resolve_proxy_url(
    base_proxy: str | None = None,
    hotel_id: str | int | None = None,
    attempt: int = 1,
) -> str | None:
    """Format proxy URL with session rotation or multi-port/pool round-robin.

    Supported patterns:
      1. Proxy list / file: Load proxies from proxies.txt or list and distribute.
      2. Multi-port Datacenter: "http://user:pass@dc.decodo.com:{port:10001-10010}"
      3. Standard format:       "dc.decodo.com:10001:user:pass"
      4. Rotating Residential:  "http://user-session-{session_id}:pass@gate.provider.com:7000"
      5. None:                  Direct connection (local IP).
    """
    pool = load_proxy_pool(base_proxy)
    if not pool:
        return None

    # Stable hash per hotel
    if hotel_id:
        h_val = int(hashlib.md5(str(hotel_id).encode("utf-8")).hexdigest()[:8], 16)
    else:
        h_val = random.randint(0, 99999)

    # If pool contains multiple distinct proxies (e.g. from file or list)
    if len(pool) > 1:
        idx = (h_val + (attempt - 1)) % len(pool)
        selected = pool[idx]
    else:
        selected = pool[0]

    # Handle multi-port range (e.g. Decodo 10 ports: {port:10001-10010})
    m = PORT_RANGE_RE.search(selected)
    if m:
        start_port = int(m.group(1))
        end_port = int(m.group(2))
        port_count = max(1, end_port - start_port + 1)
        port_offset = (h_val + (attempt - 1)) % port_count
        selected_port = start_port + port_offset
        selected = selected.replace(m.group(0), str(selected_port))

    # Handle residential session_id placeholder
    if "{session_id}" in selected:
        session_id = generate_session_id(hotel_id, attempt)
        selected = selected.replace("{session_id}", session_id)

    return selected
