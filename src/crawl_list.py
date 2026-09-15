"""Bước 3 — crawl danh sách khách sạn khi KHÔNG có API JSON.

Dùng Crawl4AI với browser profile thật + cuộn để lazy-load, rồi trích bằng CSS
selector. Đây là phương án B; nếu recon.py tìm ra API JSON thì dùng API nhanh hơn.

    python src/crawl_list.py --probe          # thử selector, chưa lưu dữ liệu
    python src/crawl_list.py                  # crawl + lưu JSON
    python src/crawl_list.py --schema class-contains-card
    python src/crawl_list.py --from-file output/html/xxx.html --probe   # offline

Kết quả: output/data/hotels_<slug>_<timestamp>.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import config
import hotel_selectors as sel
from extract import dedupe, extract, probe
from recon import slug

SCROLL_JS = """
(async () => {
  let last = 0, same = 0;
  for (let i = 0; i < %d; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, %d));
    const h = document.body.scrollHeight;
    if (h === last) { if (++same >= 3) break; } else { same = 0; last = h; }
  }
})()
"""


async def fetch_html(url: str) -> str:
    """Mở trang bằng Crawl4AI với profile thật, cuộn hết, trả HTML đã render."""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser_config = BrowserConfig(
        headless=config.HEADLESS,
        browser_type="chromium",
        use_managed_browser=True,          # bắt buộc để dùng profile lưu sẵn
        user_data_dir=str(config.PROFILE_DIR),
        verbose=True,
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        scan_full_page=True,
        wait_for_images=False,
        page_timeout=config.PAGE_TIMEOUT_MS,
        js_code=SCROLL_JS % (config.MAX_SCROLL_ROUNDS, config.SCROLL_PAUSE_MS),
        session_id="tripcom",
        check_robots_txt=config.RESPECT_ROBOTS,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)

    if not result.success:
        msg = result.error_message or "không rõ nguyên nhân"
        if result.status_code == 403 and "robots" in msg.lower():
            raise SystemExit(
                "Bị robots.txt chặn.\n"
                "Đây là quyết định của công ty, không phải của anh: hỏi anh Thắng\n"
                "trước khi đặt RESPECT_ROBOTS=false trong .env."
            )
        raise SystemExit(f"Crawl thất bại: {msg}")

    html = result.html or ""
    out = config.HTML_DIR / f"{slug(url)}.html"
    out.write_text(html, encoding="utf-8")
    print(f"→ HTML: {out} ({len(html):,} ký tự)")
    return html


def run_probe(html: str) -> None:
    print("\nThử từng bộ selector trên HTML đã render:\n")
    print(f"{'bộ selector':<26} {'card':>6} {'đủ name+url':>12}")
    print("-" * 48)
    results = probe(html, sel.CANDIDATES)
    for name, total, good, sample in results:
        print(f"{name:<26} {total:>6} {good:>12}")
    best = results[0] if results else None
    if best and best[2] > 0:
        print(f"\nBộ tốt nhất: {best[0]}. Mẫu bản ghi đầu tiên:\n")
        print(json.dumps(best[3], ensure_ascii=False, indent=2)[:1200])
        print(f"\n→ Chạy thật: python src/crawl_list.py --schema {best[0]}")
    else:
        print(
            "\nKhông bộ nào khớp. Mở file HTML trong output/html/ bằng Chrome,\n"
            "Inspect một card khách sạn, rồi sửa baseSelector trong src/hotel_selectors.py."
        )


def pick_schema(name: str | None) -> dict:
    target = name or sel.ACTIVE
    if target:
        for s in sel.CANDIDATES:
            if s["name"] == target:
                return s
        raise SystemExit(f"Không có bộ selector tên '{target}'")
    return sel.CANDIDATES[0]


async def main(args: argparse.Namespace) -> None:
    if args.from_file:
        html = Path(args.from_file).read_text(encoding="utf-8", errors="ignore")
        source = args.from_file
    else:
        html = await fetch_html(args.url)
        source = args.url

    if args.probe:
        run_probe(html)
        return

    schema = pick_schema(args.schema)
    rows = dedupe(extract(html, schema))
    if not rows:
        print("Không trích được bản ghi nào. Chạy lại với --probe.")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = config.DATA_DIR / f"hotels_{slug(source)}_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "source_url": source,
                "schema": schema["name"],
                "crawled_at": datetime.now().isoformat(timespec="seconds"),
                "count": len(rows),
                "hotels": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with_id = sum(1 for r in rows if r.get("trip_hotel_id"))
    print(f"\n→ {len(rows)} khách sạn ({with_id} có trip_hotel_id) → {out}")
    if with_id < len(rows):
        print("  Bản ghi thiếu trip_hotel_id sẽ không upsert được — kiểm tra selector 'url'.")
    print(f"\nNạp vào Postgres: python src/db/loader.py {out.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=config.TARGET_URLS["list_hcmc"])
    ap.add_argument("--schema", help="tên bộ selector trong src/hotel_selectors.py")
    ap.add_argument("--probe", action="store_true", help="chỉ thử selector, không lưu")
    ap.add_argument("--from-file", help="dùng file HTML có sẵn thay vì mở browser")
    args = ap.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main(args))
