"""Read-only browser probe for the hotel facilities panel."""
import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hotel_facilities import capture_hotel_facilities
from detail_extract import extract_detail

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale='vi-VN')
        await page.goto('https://vn.trip.com/hotels/detail/?hotelId=110585808', wait_until='domcontentloaded')
        # The complete property facilities section is already rendered in DOM.
        # Clicking matching text can select a non-interactive h2 or a hidden panel.
        try:
            await page.locator(
                '[class*="hotelFacilityNew_hotelFacilityNew"] [class*="popular_detailDesc"]'
            ).first.wait_for(state='attached', timeout=20000)
        except PlaywrightTimeoutError:
            await browser.close()
            raise SystemExit('Không tìm thấy phần tiện nghi. Trang có thể chưa tải đúng hoặc đang yêu cầu xác minh; chưa thể kiểm chứng.')
        facilities = await capture_hotel_facilities(page)
        if '--inspect-source' in sys.argv:
            evidence = await page.evaluate(r'''() => {
                const root = document.querySelector('[class*="hotelFacilityNew_hotelFacilityNew"]');
                const matches = [];
                const visit = (value, path, depth) => {
                    if (!value || typeof value !== 'object' || depth > 30) return;
                    for (const [key, child] of Object.entries(value)) {
                        const next = path + '.' + key;
                        if (/facilit|amenit/i.test(key) && !/room/i.test(next))
                            matches.push({path: next, sample: JSON.stringify(child).slice(0, 18000)});
                        else visit(child, next, depth + 1);
                    }
                };
                for (const script of document.querySelectorAll('script[type="application/json"]')) {
                    try {visit(JSON.parse(script.textContent), script.id || 'json', 0);} catch {}
                }
                const scripts = [...document.scripts].map(s => {
                    const raw = s.textContent || '';
                    const index = raw.search(/allFacilities|allPopularFacility|facilityInfo/);
                    return index >= 0 ? {type:s.type, id:s.id, sample:raw.slice(Math.max(0,index-300),index+5000)} : null;
                }).filter(Boolean).slice(0, 5);
                return {matches: matches.slice(0, 12), scripts,
                    flight: (window.__next_f || []).filter(x => typeof x[1] === 'string' && x[1].includes('hotelFacilityPopV2')).map(x=>x[1].slice(0,800))};
            }''')
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
            await browser.close()
            return
        if not facilities:
            await browser.close()
            raise SystemExit('Không thu được tiện nghi từ DOM; chưa thể kiểm chứng.')
        result = extract_detail([{"url": "embedded:hotel-facilities", "response": facilities}],
                                "110585808", page.url, "VND", "vi-VN")
        print(json.dumps(result['amenities'], ensure_ascii=False, indent=2))
        names = {item['name'] for item in result['amenities']}
        assert {'Phòng gym', 'Nơi để hành lý', 'Báo động cháy'} <= names
        assert not any('m²' in name for name in names)
        parking = next(item for item in result['amenities'] if item['code'] == '656')
        assert parking['is_available'] is False, 'Unavailable parking must remain false'
        print('VALIDATED:', len(names), 'property facilities; no room areas')
        await browser.close()


asyncio.run(main())
