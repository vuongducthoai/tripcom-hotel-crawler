"""Ghi một bundle đã qua kiểm tra vào schema v2 (gọi bên trong một transaction).

Nguyên tắc để không ghi đè dữ liệu tốt bằng dữ liệu rỗng:
  * Cột lõi (sao, tọa độ, diện tích…): COALESCE(mới, cũ) — mới là NULL thì giữ cũ.
  * Danh sách theo ngôn ngữ (ảnh, chính sách, nhãn đánh giá…): chỉ thay khi lần
    này CÓ dữ liệu; lần này rỗng thì giữ nguyên bản cũ.
  * Gói giá: không xóa — mỗi (ngày ở, ngày lấy) là một dòng, giữ lịch sử giá.
"""
from __future__ import annotations

from psycopg2.extras import Json, execute_values

from .extract import PARSER_VERSION, Bundle


def _rows(cur, sql: str, rows: list[tuple], template: str | None = None, fetch: bool = False):
    if not rows:
        return []
    return execute_values(cur, sql, rows, template=template, page_size=500, fetch=fetch) or []


def write_bundle(cur, b: Bundle) -> int:
    """Trả về v2.hotels.id. Lỗi SQL → ném ra, loader rollback cả khách sạn."""
    cur.execute("SET LOCAL search_path TO v2")
    city_id = _write_geo(cur, b)
    hotel_id = _write_hotel(cur, b, city_id)
    _write_images(cur, b, hotel_id)
    _write_amenities(cur, b, hotel_id)
    _write_policies(cur, b, hotel_id)
    room_ids = _write_rooms(cur, b, hotel_id)
    _write_offers(cur, b, room_ids)
    _write_snapshots(cur, b, hotel_id)
    _write_reviews(cur, b, hotel_id)
    _write_nearby(cur, b, hotel_id)
    cur.execute(
        """INSERT INTO hotel_crawls (hotel_id, locale, currency, crawled_at, parser_version, raw_path, sections)
           VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s, %s)""",
        (hotel_id, b.locale, b.currency, b.crawled_at, PARSER_VERSION, b.raw_path, Json(b.sections())))
    return hotel_id


# ------------------------------------------------------------------ địa lý + khách sạn
def _write_geo(cur, b: Bundle) -> int | None:
    if not (b.country and b.city):
        return None
    cur.execute("""INSERT INTO countries (trip_country_id) VALUES (%s)
                   ON CONFLICT (trip_country_id) DO UPDATE SET trip_country_id = EXCLUDED.trip_country_id
                   RETURNING id""", (b.country.trip_country_id,))
    country_id = cur.fetchone()[0]
    if b.country.name:
        cur.execute("""INSERT INTO country_i18n VALUES (%s, %s, %s)
                       ON CONFLICT (country_id, locale) DO UPDATE SET name = EXCLUDED.name""",
                    (country_id, b.locale, b.country.name))
    c = b.city
    cur.execute("""INSERT INTO cities (trip_city_id, country_id, trip_province_id, utc_offset_sec)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (trip_city_id) DO UPDATE SET
                       trip_province_id = COALESCE(EXCLUDED.trip_province_id, cities.trip_province_id),
                       utc_offset_sec = COALESCE(EXCLUDED.utc_offset_sec, cities.utc_offset_sec)
                   RETURNING id""", (c.trip_city_id, country_id, c.trip_province_id, c.utc_offset_sec))
    city_id = cur.fetchone()[0]
    if c.name:
        cur.execute("""INSERT INTO city_i18n VALUES (%s, %s, %s, %s)
                       ON CONFLICT (city_id, locale) DO UPDATE SET name = EXCLUDED.name,
                           province_name = COALESCE(EXCLUDED.province_name, city_i18n.province_name)""",
                    (city_id, b.locale, c.name, c.province_name))
    return city_id


def _write_hotel(cur, b: Bundle, city_id: int | None) -> int:
    h = b.hotel
    cur.execute(
        """INSERT INTO hotels (trip_hotel_id, city_id, star_level, star_type, is_super_star, medal_type,
                               open_year, renovated_year, room_count, latitude, longitude, is_private_host,
                               detail_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (trip_hotel_id) DO UPDATE SET
               city_id         = COALESCE(EXCLUDED.city_id, hotels.city_id),
               star_level      = COALESCE(EXCLUDED.star_level, hotels.star_level),
               star_type       = COALESCE(EXCLUDED.star_type, hotels.star_type),
               is_super_star   = COALESCE(EXCLUDED.is_super_star, hotels.is_super_star),
               medal_type      = COALESCE(EXCLUDED.medal_type, hotels.medal_type),
               open_year       = COALESCE(EXCLUDED.open_year, hotels.open_year),
               renovated_year  = COALESCE(EXCLUDED.renovated_year, hotels.renovated_year),
               room_count      = COALESCE(EXCLUDED.room_count, hotels.room_count),
               latitude        = COALESCE(EXCLUDED.latitude, hotels.latitude),
               longitude       = COALESCE(EXCLUDED.longitude, hotels.longitude),
               is_private_host = COALESCE(EXCLUDED.is_private_host, hotels.is_private_host),
               detail_url      = COALESCE(hotels.detail_url, EXCLUDED.detail_url),
               updated_at      = now()
           RETURNING id""",
        (h.trip_hotel_id, city_id, h.star_level, h.star_type, h.is_super_star, h.medal_type, h.open_year,
         h.renovated_year, h.room_count, h.latitude, h.longitude, h.is_private_host,
         h.detail_url))
    hotel_id = cur.fetchone()[0]
    t = b.hotel_i18n
    cur.execute(
        """INSERT INTO hotel_i18n (hotel_id, locale, name, local_name, address, zone_name, traffic_desc,
                                   hotel_type, description, description_source, last_booked_text)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (hotel_id, locale) DO UPDATE SET
               name = EXCLUDED.name,
               local_name   = COALESCE(EXCLUDED.local_name, hotel_i18n.local_name),
               address      = COALESCE(EXCLUDED.address, hotel_i18n.address),
               zone_name    = COALESCE(EXCLUDED.zone_name, hotel_i18n.zone_name),
               traffic_desc = COALESCE(EXCLUDED.traffic_desc, hotel_i18n.traffic_desc),
               hotel_type   = COALESCE(EXCLUDED.hotel_type, hotel_i18n.hotel_type),
               description  = COALESCE(EXCLUDED.description, hotel_i18n.description),
               description_source = CASE WHEN EXCLUDED.description IS NOT NULL
                                         THEN EXCLUDED.description_source ELSE hotel_i18n.description_source END,
               last_booked_text = COALESCE(EXCLUDED.last_booked_text, hotel_i18n.last_booked_text)""",
        (hotel_id, b.locale, t.name, t.local_name, t.address, t.zone_name, t.traffic_desc, t.hotel_type,
         t.description, t.description_source, t.last_booked_text))
    if b.highlights:
        cur.execute("DELETE FROM hotel_highlights WHERE hotel_id = %s AND locale = %s", (hotel_id, b.locale))
        _rows(cur, "INSERT INTO hotel_highlights VALUES %s",
              [(hotel_id, x.locale, x.sort_order, x.trip_tag_id, x.title, x.description, x.icon_url)
               for x in b.highlights])
    return hotel_id


# ------------------------------------------------------------------ ảnh
def _write_images(cur, b: Bundle, hotel_id: int) -> None:
    if b.image_categories:
        _rows(cur, "INSERT INTO image_categories VALUES %s ON CONFLICT DO NOTHING",
              [(c.trip_category_id,) for c in b.image_categories])
        _rows(cur, """INSERT INTO image_category_i18n VALUES %s
                      ON CONFLICT (trip_category_id, locale) DO UPDATE SET name = EXCLUDED.name""",
              [(c.trip_category_id, c.locale, c.name) for c in b.image_categories])
    if b.images:
        cur.execute("DELETE FROM hotel_images WHERE hotel_id = %s", (hotel_id,))
        _rows(cur, """INSERT INTO hotel_images (hotel_id, trip_picture_id, url, uploader, trip_category_id,
                                                sort_order, is_cover) VALUES %s""",
              [(hotel_id, i.trip_picture_id, i.url, i.uploader, i.trip_category_id, i.sort_order, i.is_cover)
               for i in b.images])


# ------------------------------------------------------------------ tiện nghi
def _write_catalog(cur, b: Bundle) -> None:
    by_code = {}
    for a in b.amenities:
        by_code.setdefault(a.trip_amenity_id, a)
    categories = {a.trip_category_id: a.category_name for a in by_code.values() if a.trip_category_id is not None}
    _rows(cur, "INSERT INTO amenity_categories VALUES %s ON CONFLICT DO NOTHING",
          [(c,) for c in categories])
    _rows(cur, """INSERT INTO amenity_category_i18n VALUES %s
                  ON CONFLICT (trip_category_id, locale) DO UPDATE SET name = EXCLUDED.name""",
          [(c, b.locale, n) for c, n in categories.items() if n])
    _rows(cur, """INSERT INTO amenities (trip_amenity_id, trip_category_id) VALUES %s
                  ON CONFLICT (trip_amenity_id) DO UPDATE SET
                      trip_category_id = COALESCE(amenities.trip_category_id, EXCLUDED.trip_category_id)""",
          [(a.trip_amenity_id, a.trip_category_id) for a in by_code.values()])
    _rows(cur, """INSERT INTO amenity_i18n VALUES %s
                  ON CONFLICT (trip_amenity_id, locale) DO UPDATE SET name = EXCLUDED.name""",
          [(a.trip_amenity_id, b.locale, a.name) for a in by_code.values()])


def _write_amenities(cur, b: Bundle, hotel_id: int) -> None:
    _write_catalog(cur, b)
    if not b.hotel_amenities:
        return
    _rows(cur, """INSERT INTO hotel_amenities VALUES %s
                  ON CONFLICT (hotel_id, trip_amenity_id) DO UPDATE SET
                      is_available = EXCLUDED.is_available, is_popular = EXCLUDED.is_popular,
                      fee = COALESCE(EXCLUDED.fee, hotel_amenities.fee)""",
          [(hotel_id, a.trip_amenity_id, a.is_available, a.is_popular, a.fee) for a in b.hotel_amenities])
    cur.execute("DELETE FROM hotel_amenity_details WHERE hotel_id = %s AND locale = %s", (hotel_id, b.locale))
    _rows(cur, "INSERT INTO hotel_amenity_details VALUES %s",
          [(hotel_id, a.trip_amenity_id, a.locale, a.fee_label, Json(a.details))
           for a in b.hotel_amenities if a.fee_label or a.details])


# ------------------------------------------------------------------ chính sách
def _write_policies(cur, b: Bundle, hotel_id: int) -> None:
    if b.policy_sections:
        cur.execute("DELETE FROM hotel_policy_sections WHERE hotel_id = %s AND locale = %s", (hotel_id, b.locale))
        _rows(cur, "INSERT INTO hotel_policy_sections VALUES %s",
              [(hotel_id, s.locale, s.section_code, s.sort_order, s.title) for s in b.policy_sections])
        _rows(cur, "INSERT INTO hotel_policy_lines VALUES %s",
              [(hotel_id, s.locale, s.section_code, n, label, text)
               for s in b.policy_sections for n, (label, text) in enumerate(s.lines)])
    p = b.policy
    if p:
        cur.execute(
            """INSERT INTO hotel_policies (hotel_id, checkin_from, checkin_until, checkout_until, front_desk_24h,
                   min_checkin_age, children_allowed, child_min_age, child_free_max_age, extra_bed, crib,
                   breakfast_available, deposit_required, pets, service_animals, quiet_hours_from,
                   quiet_hours_until, payment_methods, parsed_from_locale)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (hotel_id) DO UPDATE SET
                   checkin_from = EXCLUDED.checkin_from, checkin_until = EXCLUDED.checkin_until,
                   checkout_until = EXCLUDED.checkout_until, front_desk_24h = EXCLUDED.front_desk_24h,
                   min_checkin_age = EXCLUDED.min_checkin_age, children_allowed = EXCLUDED.children_allowed,
                   child_min_age = EXCLUDED.child_min_age, child_free_max_age = EXCLUDED.child_free_max_age,
                   extra_bed = EXCLUDED.extra_bed, crib = EXCLUDED.crib,
                   breakfast_available = EXCLUDED.breakfast_available,
                   deposit_required = EXCLUDED.deposit_required, pets = EXCLUDED.pets,
                   service_animals = EXCLUDED.service_animals, payment_methods = EXCLUDED.payment_methods,
                   quiet_hours_from = EXCLUDED.quiet_hours_from, quiet_hours_until = EXCLUDED.quiet_hours_until,
                   parsed_from_locale = EXCLUDED.parsed_from_locale, updated_at = now()
               -- bản Anh được ưu tiên: bản Việt không ghi đè kết quả đã đọc từ bản Anh
               WHERE hotel_policies.parsed_from_locale <> 'en' OR EXCLUDED.parsed_from_locale = 'en'""",
            (hotel_id, p.checkin_from, p.checkin_until, p.checkout_until, p.front_desk_24h, p.min_checkin_age,
             p.children_allowed, p.child_min_age, p.child_free_max_age, p.extra_bed, p.crib,
             p.breakfast_available, p.deposit_required, p.pets, p.service_animals, p.quiet_hours_from,
             p.quiet_hours_until, p.payment_methods, p.parsed_from_locale))


# ------------------------------------------------------------------ phòng
def _write_rooms(cur, b: Bundle, hotel_id: int) -> dict[int, int]:
    if not b.rooms:
        return {}
    fetched = _rows(cur, """
        INSERT INTO room_types (hotel_id, trip_room_id, area_sqm, area_sqm_max, max_adults, bed_count,
            bedroom_count, bathroom_count, living_room_count, window_type, smoking, wifi, extra_bed,
            rent_type, property_type, view_id, sort_order)
        VALUES %s
        ON CONFLICT (hotel_id, trip_room_id) DO UPDATE SET
            area_sqm = COALESCE(EXCLUDED.area_sqm, room_types.area_sqm),
            area_sqm_max = COALESCE(EXCLUDED.area_sqm_max, room_types.area_sqm_max),
            max_adults = COALESCE(EXCLUDED.max_adults, room_types.max_adults),
            bed_count = COALESCE(EXCLUDED.bed_count, room_types.bed_count),
            bedroom_count = COALESCE(EXCLUDED.bedroom_count, room_types.bedroom_count),
            bathroom_count = COALESCE(EXCLUDED.bathroom_count, room_types.bathroom_count),
            living_room_count = COALESCE(EXCLUDED.living_room_count, room_types.living_room_count),
            window_type = COALESCE(EXCLUDED.window_type, room_types.window_type),
            smoking = COALESCE(EXCLUDED.smoking, room_types.smoking),
            wifi = COALESCE(EXCLUDED.wifi, room_types.wifi),
            extra_bed = COALESCE(EXCLUDED.extra_bed, room_types.extra_bed),
            rent_type = COALESCE(EXCLUDED.rent_type, room_types.rent_type),
            property_type = COALESCE(EXCLUDED.property_type, room_types.property_type),
            view_id = COALESCE(EXCLUDED.view_id, room_types.view_id),
            sort_order = COALESCE(EXCLUDED.sort_order, room_types.sort_order)
        RETURNING trip_room_id, id""",
        [(hotel_id, r.trip_room_id, r.area_sqm, r.area_sqm_max, r.max_adults, r.bed_count, r.bedroom_count,
          r.bathroom_count, r.living_room_count, r.window_type, r.smoking, r.wifi, r.extra_bed, r.rent_type,
          r.property_type, r.view_id, r.sort_order) for r in b.rooms],
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::v2.allow_state, %s, %s, %s, %s)",
        fetch=True)
    ids = {trip: rid for trip, rid in fetched}
    _rows(cur, """
        INSERT INTO room_type_i18n (room_type_id, locale, name, bed_summary, bed_details, area_text, view_text,
            guest_text, extra_bed_text, child_policy, floor_text, rent_text, house_note, special_note)
        VALUES %s
        ON CONFLICT (room_type_id, locale) DO UPDATE SET
            name = EXCLUDED.name, bed_summary = EXCLUDED.bed_summary, bed_details = EXCLUDED.bed_details,
            area_text = EXCLUDED.area_text, view_text = EXCLUDED.view_text, guest_text = EXCLUDED.guest_text,
            extra_bed_text = EXCLUDED.extra_bed_text, child_policy = EXCLUDED.child_policy,
            floor_text = EXCLUDED.floor_text, rent_text = EXCLUDED.rent_text,
            house_note = EXCLUDED.house_note, special_note = EXCLUDED.special_note""",
        [(ids[t.trip_room_id], t.locale, t.name, t.bed_summary, Json(t.bed_details), t.area_text, t.view_text,
          t.guest_text, t.extra_bed_text, t.child_policy, t.floor_text, t.rent_text, t.house_note,
          t.special_note) for t in b.room_i18n if t.trip_room_id in ids])
    with_images = sorted({ids[i.trip_room_id] for i in b.room_images if i.trip_room_id in ids})
    if with_images:
        cur.execute("DELETE FROM room_images WHERE room_type_id = ANY(%s)", (with_images,))
        _rows(cur, "INSERT INTO room_images VALUES %s ON CONFLICT DO NOTHING",
              [(ids[i.trip_room_id], i.url, i.sort_order) for i in b.room_images if i.trip_room_id in ids])
    _rows(cur, """INSERT INTO room_amenities VALUES %s
                  ON CONFLICT (room_type_id, trip_amenity_id) DO UPDATE SET
                      is_highlight = EXCLUDED.is_highlight, fee = COALESCE(EXCLUDED.fee, room_amenities.fee)""",
          [(ids[a.trip_room_id], a.trip_amenity_id, a.is_highlight, a.fee)
           for a in b.room_amenities if a.trip_room_id in ids])
    return ids


def _write_offers(cur, b: Bundle, room_ids: dict[int, int]) -> None:
    offers = [o for o in b.offers if o.trip_room_id in room_ids]
    if not offers:
        return
    fetched = _rows(cur, """
        INSERT INTO room_offers (room_type_id, trip_offer_key, trip_sale_room_id, room_code, check_in, check_out,
            adults, captured_date, meal_type, breakfast_included, cancel_type, free_cancellation,
            free_cancel_until, payment_type, instant_confirm, max_guests, remaining_rooms, is_sold_out,
            is_lowest_price, is_partner_offer)
        VALUES %s
        ON CONFLICT (trip_offer_key, check_in, check_out, adults, captured_date) DO UPDATE SET
            meal_type = COALESCE(EXCLUDED.meal_type, room_offers.meal_type),
            breakfast_included = COALESCE(EXCLUDED.breakfast_included, room_offers.breakfast_included),
            cancel_type = EXCLUDED.cancel_type, free_cancellation = EXCLUDED.free_cancellation,
            free_cancel_until = COALESCE(EXCLUDED.free_cancel_until, room_offers.free_cancel_until),
            payment_type = EXCLUDED.payment_type, instant_confirm = EXCLUDED.instant_confirm,
            max_guests = EXCLUDED.max_guests, remaining_rooms = EXCLUDED.remaining_rooms,
            is_sold_out = EXCLUDED.is_sold_out, is_lowest_price = EXCLUDED.is_lowest_price,
            is_partner_offer = EXCLUDED.is_partner_offer
        RETURNING trip_offer_key, id""",
        [(room_ids[o.trip_room_id], o.trip_offer_key, o.trip_sale_room_id, o.room_code, o.check_in, o.check_out,
          o.adults, o.captured_date, o.meal_type, o.breakfast_included, o.cancel_type, o.free_cancellation,
          o.free_cancel_until, o.payment_type, o.instant_confirm, o.max_guests, o.remaining_rooms,
          o.is_sold_out, o.is_lowest_price, o.is_partner_offer) for o in offers],
        fetch=True)
    ids = {key: oid for key, oid in fetched}
    _rows(cur, """INSERT INTO room_offer_prices (offer_id, currency, price_per_night, total_price, taxes_fees,
                                                 original_price) VALUES %s
                  ON CONFLICT (offer_id, currency) DO UPDATE SET
                      price_per_night = EXCLUDED.price_per_night, total_price = EXCLUDED.total_price,
                      taxes_fees = EXCLUDED.taxes_fees, original_price = EXCLUDED.original_price,
                      captured_at = now()""",
          [(ids[p.trip_offer_key], p.currency, p.price_per_night, p.total_price, p.taxes_fees, p.original_price)
           for p in b.offer_prices if p.trip_offer_key in ids])
    _rows(cur, """INSERT INTO room_offer_i18n (offer_id, locale, title, meal_text, cancel_title, cancel_detail,
                      payment_text, confirm_text, cancel_tiers, discount_labels, partner_text) VALUES %s
                  ON CONFLICT (offer_id, locale) DO UPDATE SET
                      title = EXCLUDED.title, meal_text = EXCLUDED.meal_text,
                      cancel_title = EXCLUDED.cancel_title, cancel_detail = EXCLUDED.cancel_detail,
                      payment_text = EXCLUDED.payment_text, confirm_text = EXCLUDED.confirm_text,
                      cancel_tiers = EXCLUDED.cancel_tiers, discount_labels = EXCLUDED.discount_labels,
                      partner_text = EXCLUDED.partner_text""",
          [(ids[t.trip_offer_key], t.locale, t.title, t.meal_text, t.cancel_title, t.cancel_detail, t.payment_text,
            t.confirm_text, Json(t.cancel_tiers), Json(t.discount_labels), t.partner_text)
           for t in b.offer_i18n if t.trip_offer_key in ids])
    tiered = [o for o in offers if o.tiers and o.trip_offer_key in ids]
    if tiered:
        cur.execute("DELETE FROM room_offer_cancel_tiers WHERE offer_id = ANY(%s)",
                    ([ids[o.trip_offer_key] for o in tiered],))
        _rows(cur, "INSERT INTO room_offer_cancel_tiers VALUES %s",
              [(ids[o.trip_offer_key], t.tier_no, t.starts_at, t.ends_at, t.penalty_ratio)
               for o in tiered for t in o.tiers])


def _write_snapshots(cur, b: Bundle, hotel_id: int) -> None:
    _rows(cur, """INSERT INTO hotel_price_snapshots VALUES %s
                  ON CONFLICT (hotel_id, check_in, check_out, currency, captured_date, source) DO UPDATE SET
                      min_price = EXCLUDED.min_price, min_total = EXCLUDED.min_total""",
          [(hotel_id, s.check_in, s.check_out, s.currency, s.captured_date, s.min_price, s.min_total, s.source)
           for s in b.snapshots])


# ------------------------------------------------------------------ đánh giá, lân cận
def _write_reviews(cur, b: Bundle, hotel_id: int) -> None:
    r = b.review
    if r:
        cur.execute(
            """INSERT INTO hotel_review_summary (hotel_id, rating_overall, rating_location, rating_facility,
                   rating_service, rating_cleanliness, review_count, rating_scale)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (hotel_id) DO UPDATE SET
                   rating_overall = EXCLUDED.rating_overall, rating_location = EXCLUDED.rating_location,
                   rating_facility = EXCLUDED.rating_facility, rating_service = EXCLUDED.rating_service,
                   rating_cleanliness = EXCLUDED.rating_cleanliness, review_count = EXCLUDED.review_count,
                   rating_scale = EXCLUDED.rating_scale, captured_at = now()""",
            (hotel_id, r.rating_overall, r.rating_location, r.rating_facility, r.rating_service,
             r.rating_cleanliness, r.review_count, r.rating_scale))
        cur.execute("""INSERT INTO hotel_review_summary_i18n VALUES (%s, %s, %s, %s)
                       ON CONFLICT (hotel_id, locale) DO UPDATE SET level_text = EXCLUDED.level_text,
                           ai_summary = COALESCE(EXCLUDED.ai_summary, hotel_review_summary_i18n.ai_summary)""",
                    (hotel_id, b.locale, r.level_text, Json(r.ai_summary) if r.ai_summary else None))
    if b.review_tags:
        cur.execute("DELETE FROM hotel_review_tags WHERE hotel_id = %s AND locale = %s", (hotel_id, b.locale))
        _rows(cur, "INSERT INTO hotel_review_tags VALUES %s",
              [(hotel_id, t.locale, t.trip_tag_id, t.name, t.mention_count, t.sentiment) for t in b.review_tags])


def _write_nearby(cur, b: Bundle, hotel_id: int) -> None:
    if not b.nearby:
        return
    fetched = _rows(cur, """INSERT INTO places (trip_poi_id, poi_type, latitude, longitude) VALUES %s
                            ON CONFLICT (trip_poi_id) DO UPDATE SET
                                poi_type = COALESCE(EXCLUDED.poi_type, places.poi_type),
                                latitude = COALESCE(EXCLUDED.latitude, places.latitude),
                                longitude = COALESCE(EXCLUDED.longitude, places.longitude)
                            RETURNING trip_poi_id, id""",
                    [(p.trip_poi_id, p.poi_type, p.latitude, p.longitude) for p in b.nearby], fetch=True)
    ids = {poi: pid for poi, pid in fetched}
    _rows(cur, """INSERT INTO place_i18n VALUES %s
                  ON CONFLICT (place_id, locale) DO UPDATE SET name = EXCLUDED.name,
                      kind = COALESCE(EXCLUDED.kind, place_i18n.kind)""",
          [(ids[p.trip_poi_id], p.locale, p.name, p.kind) for p in b.nearby])
    _rows(cur, """INSERT INTO hotel_nearby_places VALUES %s
                  ON CONFLICT (hotel_id, place_id) DO UPDATE SET group_code = EXCLUDED.group_code,
                      distance_km = EXCLUDED.distance_km, travel_mode = EXCLUDED.travel_mode,
                      sort_order = EXCLUDED.sort_order""",
          [(hotel_id, ids[p.trip_poi_id], p.group_code, p.distance_km, p.travel_mode, p.sort_order)
           for p in b.nearby])
    _rows(cur, """INSERT INTO hotel_nearby_place_i18n VALUES %s
                  ON CONFLICT (hotel_id, place_id, locale) DO UPDATE SET
                      group_name = EXCLUDED.group_name, distance_text = EXCLUDED.distance_text""",
          [(hotel_id, ids[p.trip_poi_id], p.locale, p.group_name, p.distance_text) for p in b.nearby])
