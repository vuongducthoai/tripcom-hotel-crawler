"""Bộ CSS selector ứng viên cho card khách sạn trên trang danh sách Trip.com.

Trip.com đổi class thường xuyên và class của họ là hash sinh tự động, nên
KHÔNG có bộ selector nào đúng vĩnh viễn. Cách dùng:

    python src/crawl_list.py --probe

Script sẽ thử lần lượt từng CANDIDATE bên dưới trên HTML đã render và in ra
bộ nào bắt được nhiều card nhất. Sau đó anh sửa/bổ sung trực tiếp vào đây.

Cách tìm selector thật: mở trang trên Chrome → chuột phải vào một card khách
sạn → Inspect → tìm thẻ cha bao trọn card → copy class hoặc data-attribute.
Ưu tiên `data-*` và `[class*="..."]` vì bền hơn class hash.
"""
from __future__ import annotations

# Mỗi schema: baseSelector chọn ra từng CARD, fields là các mẩu bên trong card.
# type: "text" | "attr:<tên>" | "html"
CANDIDATES: list[dict] = [
    {
        "name": "data-attr",
        "baseSelector": "[data-testid*='hotel'], [data-exposure*='hotel']",
        "fields": [
            {"name": "name", "selector": "[data-testid*='name'], h3, h2", "type": "text"},
            {"name": "url", "selector": "a[href*='/hotels/']", "type": "attr:href"},
            {"name": "price", "selector": "[data-testid*='price'], [class*='price']", "type": "text"},
            {"name": "score", "selector": "[class*='score'], [class*='rating']", "type": "text"},
            {"name": "address", "selector": "[class*='address'], [class*='location']", "type": "text"},
            {"name": "image", "selector": "img", "type": "attr:src"},
        ],
    },
    {
        "name": "class-contains-card",
        "baseSelector": "[class*='hotel-card'], [class*='hotelCard'], [class*='list-card']",
        "fields": [
            {"name": "name", "selector": "[class*='name'], h3, h2", "type": "text"},
            {"name": "url", "selector": "a[href*='/hotels/']", "type": "attr:href"},
            {"name": "price", "selector": "[class*='price']", "type": "text"},
            {"name": "score", "selector": "[class*='score'], [class*='rating']", "type": "text"},
            {"name": "address", "selector": "[class*='address'], [class*='zone']", "type": "text"},
            {"name": "image", "selector": "img", "type": "attr:src"},
        ],
    },
    {
        "name": "li-generic",
        "baseSelector": "li:has(a[href*='/hotels/detail'])",
        "fields": [
            {"name": "name", "selector": "h3, h2, [class*='name']", "type": "text"},
            {"name": "url", "selector": "a[href*='/hotels/']", "type": "attr:href"},
            {"name": "price", "selector": "[class*='price']", "type": "text"},
            {"name": "score", "selector": "[class*='score']", "type": "text"},
            {"name": "image", "selector": "img", "type": "attr:src"},
        ],
    },
    {
        "name": "anchor-only",  # fallback thô: mọi link tới trang chi tiết
        "baseSelector": "a[href*='/hotels/detail']",
        "fields": [
            {"name": "name", "selector": "self", "type": "attr:title"},
            {"name": "url", "selector": "self", "type": "attr:href"},
        ],
    },
]

# Sau khi probe xong, ghi tên bộ thắng vào đây để crawl_list.py dùng mặc định.
ACTIVE: str | None = None
