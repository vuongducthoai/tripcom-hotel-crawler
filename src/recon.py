"""Bước 2 — RECON: tìm API JSON nội bộ của Trip.com.

Đây là script quan trọng nhất của cả dự án. Nó mở trang danh sách khách sạn
bằng profile thật, tự cuộn để kích hoạt lazy-load, và ghi lại TOÀN BỘ request
XHR/fetch cùng response JSON.

Nếu tìm được một endpoint trả về danh sách khách sạn dạng JSON, anh không cần
Crawl4AI, không cần browser, không cần parse HTML — chỉ cần httpx gọi thẳng
endpoint đó. Nhanh hơn 5–10 lần và ổn định hơn nhiều.

    python src/recon.py
    python src/recon.py --url https://vn.trip.com/hotels/list?city=359

Kết quả:
    output/recon/summary.md        bảng endpoint, sắp theo kích thước response
    output/recon/bodies/NNN_*.json response body đầy đủ để anh đọc
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Response, async_playwright

import config

# Bỏ qua tiếng ồn: analytics, ads, log, tracking
NOISE = re.compile(
    r"(google|gstatic|doubleclick|facebook|criteo|sentry|hotjar|clarity|"
    r"analytics|beacon|/log|/track|adsystem|tiktok|bat\.bing)",
    re.I,
)

MAX_BODY_BYTES = 4_000_000


def slug(url: str) -> str:
    p = urlparse(url)
    s = f"{p.netloc}{p.path}".replace("/", "_").strip("_")
    return re.sub(r"[^A-Za-z0-9._-]", "", s)[:90] or "root"


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.bodies_dir = config.RECON_DIR / "bodies"
        self.bodies_dir.mkdir(parents=True, exist_ok=True)
        self._n = 0
        self._seen: set[str] = set()

    async def on_response(self, resp: Response) -> None:
        req = resp.request
        if req.resource_type not in ("xhr", "fetch"):
            return
        if NOISE.search(req.url):
            return

        ctype = (resp.headers.get("content-type") or "").lower()
        try:
            body = await resp.body()
        except Exception:
            return
        if not body or len(body) > MAX_BODY_BYTES:
            return

        # Chỉ quan tâm JSON — dữ liệu khách sạn gần như chắc chắn nằm ở đây.
        is_json = "json" in ctype or body[:1] in (b"{", b"[")
        if not is_json:
            return

        try:
            parsed = json.loads(body)
        except Exception:
            return

        key = f"{req.method} {req.url.split('?')[0]}"
        dup = key in self._seen
        self._seen.add(key)

        self._n += 1
        fname = f"{self._n:03d}_{slug(req.url)}.json"
        payload = {
            "url": req.url,
            "method": req.method,
            "status": resp.status,
            "request_headers": dict(req.headers),
            "request_post_data": req.post_data,
            "response": parsed,
        }
        (self.bodies_dir / fname).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.rows.append(
            {
                "n": self._n,
                "method": req.method,
                "url": req.url,
                "status": resp.status,
                "bytes": len(body),
                "file": fname,
                "repeat": dup,
                "hints": hint_fields(parsed),
            }
        )

    def write_summary(self, target: str) -> None:
        rows = sorted(self.rows, key=lambda r: r["bytes"], reverse=True)
        lines = [
            "# Recon Trip.com",
            "",
            f"- Trang: {target}",
            f"- Thời điểm: {datetime.now().isoformat(timespec='seconds')}",
            f"- Số response JSON bắt được: {len(rows)}",
            "",
            "Sắp theo kích thước giảm dần. Endpoint to nhất thường chính là",
            "endpoint trả danh sách khách sạn — mở file tương ứng trong bodies/ để xác nhận.",
            "",
            "| # | Bytes | Status | Method | Endpoint | Gợi ý field | File |",
            "|---|-------|--------|--------|----------|-------------|------|",
        ]
        for r in rows:
            short = r["url"].split("?")[0]
            if len(short) > 80:
                short = "…" + short[-79:]
            lines.append(
                f"| {r['n']} | {r['bytes']:,} | {r['status']} | {r['method']} | "
                f"`{short}` | {r['hints'] or '—'} | `{r['file']}` |"
            )
        lines += [
            "",
            "## Việc tiếp theo",
            "",
            "1. Mở 3 file đầu bảng, tìm mảng chứa tên/giá khách sạn.",
            "2. Nếu thấy → ghi lại đúng endpoint + params phân trang, rồi gọi bằng httpx.",
            "3. Nếu KHÔNG thấy (dữ liệu nằm trong HTML/SSR) → chuyển sang crawl_list.py.",
            "4. Copy bảng này vào docs/recon.md và điền kết luận.",
            "",
        ]
        out = config.RECON_DIR / "summary.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n→ {out}")


HOTEL_KEYS = (
    "hotel", "hotels", "hotelList", "hotelId", "hotelName",
    "price", "amount", "starRating", "reviewScore", "commentScore",
    "latitude", "longitude", "roomList", "cityId",
)


def hint_fields(obj, depth: int = 0) -> str:
    """Dò nông xem response có chứa key nghi là dữ liệu khách sạn không."""
    found: set[str] = set()

    def walk(o, d: int) -> None:
        if d > 6 or len(found) > 6:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in HOTEL_KEYS:
                    found.add(k)
                walk(v, d + 1)
        elif isinstance(o, list):
            for v in o[:5]:
                walk(v, d + 1)

    walk(obj, depth)
    return ", ".join(sorted(found))


SCROLL_JS = """
(async () => {
  let last = 0, same = 0;
  for (let i = 0; i < %d; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, %d));
    const h = document.body.scrollHeight;
    if (h === last) { if (++same >= 3) break; } else { same = 0; last = h; }
  }
  window.scrollTo(0, 0);
})()
"""


async def main(url: str, locale: str | None = None, profile_dir: str | None = None) -> None:
    rec = Recorder()
    locale = locale or config.LOCALE
    # Mỗi thị trường có profile riêng: recon tiếng Anh phải dùng đúng profile
    # tiếng Anh, không thì cookie/token lấy được sẽ không khớp lúc cào thật.
    profile = Path(profile_dir) if profile_dir else config.PROFILE_DIR
    if not profile.is_absolute():
        profile = config.ROOT / profile
    print(f"Recon: {url}")
    print(f"Profile: {profile}  |  locale: {locale}")
    print("Đang mở browser với profile đã lưu…\n")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=config.HEADLESS,
            locale=locale,
            timezone_id=config.TIMEZONE,
            viewport=config.VIEWPORT,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(rec.on_response(r)))

        await page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT_MS)
        await page.wait_for_timeout(5000)

        print("Đang cuộn để kích hoạt lazy-load…")
        try:
            await page.evaluate(
                SCROLL_JS % (config.MAX_SCROLL_ROUNDS, config.SCROLL_PAUSE_MS)
            )
        except Exception as e:  # trang có thể điều hướng giữa chừng
            print(f"  (cuộn dừng sớm: {e})")

        await page.wait_for_timeout(3000)

        # Lưu luôn HTML sau khi render, phòng khi phải parse DOM.
        html = await page.content()
        html_path = config.HTML_DIR / f"{slug(url)}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"→ HTML đã render: {html_path} ({len(html):,} ký tự)")

        await ctx.close()

    # Chờ các task ghi body còn đang chạy
    await asyncio.sleep(1)

    if not rec.rows:
        print("\n⚠ Không bắt được response JSON nào.")
        print("  Nghĩa là dữ liệu có thể render sẵn trong HTML (SSR).")
        print("  → Chạy: python src/crawl_list.py --probe")
        return

    rec.write_summary(url)
    print(f"→ {len(rec.rows)} response JSON trong {rec.bodies_dir}")
    print("\nMở output/recon/summary.md, xem 3 dòng đầu bảng.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=config.TARGET_URLS["list_hcmc"])
    ap.add_argument("--locale", help="vi-VN hoặc en-US; mặc định lấy từ .env")
    ap.add_argument("--profile-dir", help="profile Chromium; mặc định browser_profile")
    args = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args.url, args.locale, args.profile_dir))
