"""Kiểm tra bộ kiểm tra dữ liệu v2 (src/v2) bằng raw giả lập — không cần DB, không cần mạng.

    python -m unittest tests.test_v2_validation -v

Mỗi ca ứng với một lỗi đã gặp thật hoặc một quy tắc trong src/v2/rules.py.
"""
from __future__ import annotations

import copy
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from v2.extract import blocked_reason, build_bundle, pair_check  # noqa: E402
from v2.rules import GATE, RULES, Issues  # noqa: E402

ROOM = "https://www.trip.com/restapi/soa2/33269/getHotelRoomListOversea"
POP = "https://www.trip.com/restapi/soa2/35021/getHotelRoomPopInfoPCOnline"
ALBUM = "https://www.trip.com/restapi/soa2/28820/ctgethotelalbum"
COMMENT = "https://www.trip.com/restapi/soa2/34308/getHotelCommentInfo"
NEARBY = "https://www.trip.com/restapi/soa2/28820/ctGetNearbyPlaceInfo"


def sale(sale_id=1001, code="AAA-1", room=501, price=85, total=91.32, tax=6.76, delete=0,
         remain=9999, currency="USD", title="Room only", cancel="Non-refundable"):
    return {
        "id": sale_id, "roomCode": code, "physicalRoomId": room,
        "priceInfo": {"price": price, "deletePricewithOutCurrency": delete, "currency": currency},
        "comparingAmount": total, "totalPriceInfo": {"payTax": {"price": tax}, "isFreeCancel": False},
        "bookingStatusInfo": {"remainRoomQuantity": remain, "isFullRoom": False},
        "titleInfo": {"title": title}, "cancelInfo": {"type": 5, "title": cancel, "ladderDetailInfo": []},
        "paymentInfo": {"type": 0, "paymentTitleNew": "Prepay online"},
        "confirmInfo": {"type": 2, "title": "Confirmed within 12 hours"},
        "guestCountInfo": {"guestCount": 2},
    }


def make_dump(locale="en-US", currency="USD", hotel="134013415"):
    """Raw tối thiểu giống cấu trúc crawler lưu (responses + normalized)."""
    vi = locale == "vi-VN"
    host = "vn.trip.com" if vi else "www.trip.com"
    return {
        "url": f"https://{host}/hotels/detail/?hotelId={hotel}&checkIn=2026-10-01&checkOut=2026-10-02"
               f"&adult=2&curr={currency}&locale={locale}",
        "target": {"trip_hotel_id": hotel},
        "normalized": {"trip_hotel_id": hotel, "name": "Test Hotel", "address": "1 Test St",
                       "description": None, "hotel_type": "Hotel", "locale": locale, "currency": currency,
                       "check_in": "2026-10-01", "check_out": "2026-10-02",
                       "crawled_at": "2026-09-20T10:00:00", "amenities": [
                           {"code": "102", "name": "Wi-Fi", "category": "Internet", "category_code": "2",
                            "is_highlight": True, "is_available": True}]},
        "responses": [
            {"url": ROOM, "response": {"data": {
                "physicRoomMap": {"501": {
                    "id": 501, "name": "Phòng Tiêu chuẩn" if vi else "Standard Room",
                    "areaInfo": {"title": "236–322 ft²"},
                    "houseTypeInfo": {"bedCount": 1, "bedRoomCount": -1, "bathRoomCount": -1, "rentType": 2},
                    "smokeInfo": {"type": 2}, "wifiInfo": {"title": "Free Wi-Fi"},
                    "floorInfo": {"title": "Floor: 1-4"}, "physicRank": 1,
                    "pictureInfo": [{"url": "https://ak-d.tripcdn.com/images/abc_R_160_90_R5_D.jpg"}],
                    "faciltityInfo": {"list": [{"id": 20, "title": "Cleaning", "subList": [
                        {"id": 606, "title": "Daily housekeeping", "freeType": 0}]}]},
                }},
                "saleRoomMap": {"1001_AAA-1": sale(currency=currency,
                                                   title="Chỉ tiền phòng" if vi else "Room only",
                                                   cancel="Không hoàn tiền" if vi else "Non-refundable")},
            }}},
            {"url": POP, "response": {"data": {"roomPopInfo": {"501": {
                "roomBasicInfo": {"guestInfo": {"content": "2 adults"}},
                "policyInfo": {"childPolicy": {"textList": ["Children welcome"]}}}}}}},
            {"url": ALBUM, "response": {"data": {"hotelImagePop": {"hotelProvide": {"imgTabs": [
                {"categoryId": -1, "categoryName": "Nổi bật" if vi else "Featured", "imgUrlList": [{"subImgUrlList": [
                    {"link": "https://ak-d.tripcdn.com/images/img1_R_960_660_R5_D.jpg", "pictureId": 1},
                    {"link": "https://ak-d.tripcdn.com/images/img1_Z_320_220_R5_D.jpg", "pictureId": 1},
                ]}]}]}}}}},
            {"url": COMMENT, "response": {"data": {"totalCount": 54, "commentRating": {
                "fullRating": 10, "ratingAll": 9, "ratingLocation": 8.8,
                "ratingLocationShowItem": "Vị trí" if vi else "Location",
                "ratingFacilityShowItem": "Tiện nghi" if vi else "Facilities",
                "ratingServiceShowItem": "Dịch vụ" if vi else "Service"},
                "commentTagList": [{"id": 29, "name": "Large rooms", "commentCount": 107, "type": 1}]}}},
            {"url": NEARBY, "response": {"data": {"placeInfoList": [{"id": 2, "name": "Giao thông" if vi else "Transport",
                "places": [{"id": 149854559, "name": "Opera House", "distance": 5.5895, "lat": 10.77, "lng": 106.70,
                            "arrivalType": "LINEAR_DISTANCE",
                            "distanceDesc": "Khoảng cách theo đường thẳng: 5,6km" if vi else "Straight line: 5.6km"}]}]}}},
        ],
    }


def run(dump, locale="en-US", currency="USD", hotel="134013415", detail=None):
    return build_bundle(dump, raw_locale=locale, currency=currency, file_hotel_id=hotel, detail=detail)


def rules_of(issues: Issues) -> set[str]:
    return {i.rule for i in issues.items}


class Layer1Response(unittest.TestCase):
    def test_clean_raw_passes(self):
        bundle, issues = run(make_dump())
        self.assertIsNotNone(bundle)
        self.assertEqual(issues.errors(), [])

    def test_4030_rejects_whole_hotel(self):
        dump = make_dump()
        dump["responses"][0]["response"] = {"htlSpiderActionErrorCode": 4030, "data": {}}
        bundle, issues = run(dump)
        self.assertIsNone(bundle)
        self.assertIn("blocked", rules_of(issues))

    def test_xor_antibot_array_is_blocked(self):
        message = '{"failedcause":"Antibot-Gray-ip"}' + " " * 40
        self.assertEqual(blocked_reason([ord(ch) ^ 0x0A for ch in message]), "Antibot")

    def test_plain_int_list_is_not_blocked(self):
        # qualityFacilityIds [107, 92, 87] từng bị bắt nhầm là Antibot
        self.assertIsNone(blocked_reason({"qualityFacilityIds": [107, 92, 87] * 30}))

    def test_vietnamese_raw_loaded_as_english_is_rejected(self):
        dump = make_dump("vi-VN", "VND")
        dump["normalized"]["locale"] = "en-US"                  # khai sai
        dump["url"] = dump["url"].replace("locale=vi-VN", "locale=en-US")
        bundle, issues = run(dump)                              # nạp như bản Anh
        self.assertIsNone(bundle)
        self.assertIn("wrong_locale", rules_of(issues))

    def test_wrong_currency_rejected(self):
        bundle, issues = run(make_dump(), currency="VND")
        self.assertIsNone(bundle)
        self.assertIn("wrong_currency", rules_of(issues))

    def test_file_name_and_raw_id_must_match(self):
        bundle, issues = run(make_dump(hotel="111"), hotel="222")
        self.assertIsNone(bundle)
        self.assertIn("hotel_id_mismatch", rules_of(issues))


class Layer2Records(unittest.TestCase):
    def test_auto_fixes(self):
        bundle, issues = run(make_dump())
        room = bundle.rooms[0]
        self.assertEqual((room.area_sqm, room.area_sqm_max), (Decimal("21.93"), Decimal("29.91")))
        self.assertIsNone(room.bedroom_count)                   # -1 → NULL
        self.assertIsNone(bundle.offers[0].remaining_rooms)     # 9999 → NULL
        self.assertEqual(bundle.images[0].url, "https://ak-d.tripcdn.com/images/img1.jpg")
        self.assertEqual(len(bundle.images), 1)                 # hai cỡ của cùng một ảnh
        for rule in ("fix_area_unit", "fix_area_range", "fix_negative_count", "fix_remaining_9999",
                     "fix_image_original", "fix_image_duplicate"):
            self.assertIn(rule, issues.fixed)

    def test_area_units(self):
        dump = make_dump("vi-VN", "VND")
        dump["responses"][0]["response"]["data"]["physicRoomMap"]["501"]["areaInfo"]["title"] = "1.076 ft²"
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"] = sale(
            currency="VND", price=1_666_667, total=1_800_000, tax=133_333, title="Chỉ tiền phòng",
            cancel="Không hoàn tiền")
        bundle, _ = run(dump, "vi-VN", "VND")
        self.assertEqual(bundle.rooms[0].area_sqm, Decimal("99.96"))
        dump["responses"][0]["response"]["data"]["physicRoomMap"]["501"]["areaInfo"]["title"] = "22㎡"
        bundle, _ = run(dump, "vi-VN", "VND")
        self.assertEqual(bundle.rooms[0].area_sqm, Decimal("22.00"))

    def test_implausible_area_becomes_null_with_warning(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["physicRoomMap"]["501"]["areaInfo"]["title"] = "1 m²"
        bundle, issues = run(dump)
        self.assertIsNone(bundle.rooms[0].area_sqm)
        self.assertIn("area_implausible", rules_of(issues))

    def test_zero_price_offer_dropped(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"]["priceInfo"]["price"] = 0
        bundle, issues = run(dump)
        self.assertEqual(bundle.offers, [])
        self.assertIn("price_not_positive", rules_of(issues))

    def test_member_only_hidden_price_is_warning_not_error(self):
        dump = make_dump()
        offer = dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"]
        offer["priceInfo"].update(price=0, displayPrice="$?")
        offer["comparingAmount"] = 0
        offer["bookingStatusInfo"]["isHidePrice"] = True
        bundle, issues = run(dump)
        self.assertEqual(bundle.offers, [])
        self.assertIn("price_hidden", rules_of(issues))
        self.assertNotIn("price_not_positive", rules_of(issues))

    def test_tax_above_total_dropped(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"] = sale(tax=500)
        bundle, issues = run(dump)
        self.assertEqual(bundle.offers, [])
        self.assertIn("tax_above_total", rules_of(issues))

    def test_strike_price_not_above_price_is_removed(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"] = sale(delete=80)
        bundle, issues = run(dump)
        self.assertIsNone(bundle.offer_prices[0].original_price)
        self.assertIn("fix_original_price", issues.fixed)

    def test_promo_sentence_is_not_a_description(self):
        dump = make_dump()
        dump["normalized"]["description"] = "Book your stay at Test Hotel in Ho Chi Minh City, rated 9.1…"
        bundle, issues = run(dump)
        self.assertIsNone(bundle.hotel_i18n.description)
        self.assertIn("promo_description", rules_of(issues))

    def test_missing_name_rejects_hotel(self):
        dump = make_dump()
        dump["normalized"]["name"] = "   "
        bundle, issues = run(dump)
        self.assertIsNone(bundle)
        self.assertIn("missing_name", rules_of(issues))

    def test_rating_above_scale_dropped(self):
        dump = make_dump()
        dump["responses"][3]["response"]["data"]["commentRating"]["ratingAll"] = 11
        bundle, issues = run(dump)
        self.assertIsNone(bundle.review)
        self.assertIn("rating_out_of_scale", rules_of(issues))

    def test_straight_line_distance_kept(self):
        bundle, _ = run(make_dump())
        self.assertEqual(bundle.nearby[0].travel_mode, "straight_line")


class Layer3Links(unittest.TestCase):
    def test_orphan_offer_dropped(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["9_X"] = sale(sale_id=9, code="X", room=999)
        bundle, issues = run(dump)
        self.assertEqual([o.trip_offer_key for o in bundle.offers], ["1001_AAA-1"])
        self.assertIn("orphan_offer", rules_of(issues))

    def test_same_sale_id_different_room_code_are_two_offers(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_BBB-2"] = sale(code="BBB-2", price=120,
                                                                                    total=130, tax=10)
        bundle, _ = run(dump)
        self.assertEqual(len(bundle.offers), 2)

    def test_popup_keyed_by_offer_key_is_matched(self):
        dump = make_dump()
        pops = dump["responses"][1]["response"]["data"]["roomPopInfo"]
        pops["1001_AAA-1"] = pops.pop("501")                    # Trip.com có lúc khóa theo gói
        bundle, issues = run(dump)
        self.assertEqual(bundle.rooms[0].max_adults, 2)
        self.assertNotIn("popup_missing", rules_of(issues))

    def test_pair_check_flags_different_checkin(self):
        en, _ = run(make_dump())
        vi_dump = make_dump("vi-VN", "VND")
        vi_dump["normalized"]["check_in"], vi_dump["normalized"]["check_out"] = "2026-09-28", "2026-09-29"
        vi_dump["responses"][0]["response"]["data"]["saleRoomMap"]["1001_AAA-1"] = sale(
            currency="VND", price=2_000_000, total=2_100_000, tax=100_000, title="Chỉ tiền phòng",
            cancel="Không hoàn tiền")
        vi, _ = run(vi_dump, "vi-VN", "VND")
        issues = Issues()
        pair_check(en, vi, issues)
        self.assertIn("pair_checkin_mismatch", rules_of(issues))


class DetailBlock(unittest.TestCase):
    DETAIL = {
        "hotelBaseInfo": {"masterHotelId": 134013415, "nameInfo": {"name": "Test Hotel",
                          "localNameTip": "Local hotel name: Khách sạn Thử"},
                          "starInfo": {"level": 3, "type": "star", "superStar": 0},
                          "openYear": "2020", "fitmentYear": "2026", "countryId": 111, "cityId": 301,
                          "countryName": "Vietnam", "cityName": "Ho Chi Minh City", "timeOffset": 25200},
        "hotelPositionInfo": {"address": "51 Co Giang", "lat": "10.764058", "lng": "106.696219"},
        "hotelDescriptionInfo": {"description": "A cozy hotel in District 1."},
        "hotelPolicyInfo": {
            "checkInAndOut": {"title": "Check-in and Check-out Times", "content": [
                {"title": "Check-in: ", "description": "After 14:00"},
                {"title": "Check-out: ", "description": "Before 12:00"},
                {"description": "Front desk hours: 24/7"}]},
            "pet": {"title": "Pets", "content": [{"description": "Pets are not allowed"}]},
            "ageLimit": {"title": "Age Requirements", "content": [
                {"description": "The main guest checking in must be at least 18 years old"}]},
        },
    }

    def test_structured_policy_and_basics(self):
        bundle, issues = run(make_dump(), detail=copy.deepcopy(self.DETAIL))
        self.assertEqual(bundle.hotel.star_level, 3)
        self.assertAlmostEqual(bundle.hotel.latitude, 10.764058)
        self.assertEqual(bundle.hotel_i18n.local_name, "Khách sạn Thử")
        self.assertEqual(bundle.hotel_i18n.description_source, "intro")
        p = bundle.policy
        self.assertEqual((str(p.checkin_from), str(p.checkout_until)), ("14:00:00", "12:00:00"))
        self.assertTrue(p.front_desk_24h)
        self.assertEqual((p.min_checkin_age, p.pets), (18, "no"))
        labels = [label for s in bundle.policy_sections if s.section_code == "checkInAndOut" for label, _ in s.lines]
        self.assertEqual(labels[:2], ["Check-in", "Check-out"])   # tách đúng, không dính "Sau 14:00Trả phòng"
        self.assertNotIn("no_detail_block", rules_of(issues))

    def test_detail_of_other_hotel_ignored(self):
        detail = copy.deepcopy(self.DETAIL)
        detail["hotelBaseInfo"]["masterHotelId"] = 999
        bundle, issues = run(make_dump(), detail=detail)
        self.assertIsNone(bundle.hotel.star_level)
        self.assertIn("detail_mismatch", rules_of(issues))


class VietnamesePolicy(unittest.TestCase):
    """Chữ thật từ trang vn.trip.com của hotel 134013415 (cào 22/9/2026)."""
    DETAIL = {
        "hotelBaseInfo": {"masterHotelId": 134013415, "nameInfo": {
            "name": "Nice Stay near SGN Airport by Sy's Home",
            "localNameTip": "Tên khách sạn địa phương: Nice Stay near SGN Airport by Sy's Home"},
            "starInfo": {"level": 3, "type": "circle", "superStar": 0}},
        "hotelPolicyInfo": {
            "checkInAndOut": {"title": "Thời gian nhận và trả phòng", "content": [
                {"title": "Nhận phòng: ", "description": "Sau 14:00"},
                {"title": "Trả phòng: ", "description": "Trước 12:00"},
                {"description": "Giờ làm việc của quầy lễ tân: 24/7"}]},
            "childPolicy": {"title": "Chính sách cho trẻ em", "content": [
                {"description": "Trẻ em 5 tuổi trở lên được chào đón tại khách sạn này."}]},
            "cribAndExtraBed": {"title": "Nôi/cũi và giường phụ", "content": [
                {"description": "Mọi loại phòng đều không thể bổ sung cũi và giường phụ."}]},
            "breakfast": {"title": "Bữa sáng", "content": [{"description": "Không có bữa sáng."}]},
            "deposit": {"title": "Chính sách đặt cọc", "content": [
                {"title": "Đặt cọc", "description": "Chỗ nghỉ không yêu cầu đặt cọc"}]},
            "pet": {"title": "Thú cưng", "content": [{"description": "Không được phép mang theo thú cưng"}]},
            "serviceAnimal": {"title": "Động vật hỗ trợ", "content": [
                {"description": "Được phép mang theo động vật hỗ trợ nếu có yêu cầu"}]},
            "quiteTime": {"title": "Thời gian yên tĩnh", "content": [
                {"description": "Khách được yêu cầu giữ yên tĩnh từ 22:00 đến 06:00"}]},
            "credit": {"title": "Thanh toán tại khách sạn", "cashType": 1, "cashDesc": "Tiền mặt"},
        },
    }

    def test_vietnamese_policy_is_normalized(self):
        bundle, _ = run(make_dump("vi-VN", "VND"), "vi-VN", "VND", detail=copy.deepcopy(self.DETAIL))
        p = bundle.policy
        self.assertEqual(p.parsed_from_locale, "vi")
        self.assertEqual((str(p.checkin_from), str(p.checkout_until)), ("14:00:00", "12:00:00"))
        self.assertTrue(p.front_desk_24h)
        self.assertEqual((p.children_allowed, p.child_min_age), ("yes", 5))
        self.assertEqual((p.extra_bed, p.crib), ("no", "no"))
        self.assertFalse(p.breakfast_available)
        self.assertFalse(p.deposit_required)
        self.assertEqual((p.pets, p.service_animals), ("no", "on_request"))
        self.assertEqual((str(p.quiet_hours_from), str(p.quiet_hours_until)), ("22:00:00", "06:00:00"))
        self.assertEqual(p.payment_methods, ["cash"])

    def test_circle_rating_and_duplicate_local_name(self):
        bundle, _ = run(make_dump("vi-VN", "VND"), "vi-VN", "VND", detail=copy.deepcopy(self.DETAIL))
        self.assertEqual((bundle.hotel.star_level, bundle.hotel.star_type), (3, "circle"))
        self.assertIsNone(bundle.hotel_i18n.local_name)      # trùng tên hiển thị → không lưu lặp

    def test_trip_sold_out_flag_does_not_hide_offers(self):
        dump = make_dump()
        dump["responses"][0]["response"]["data"]["isRoomListSoldOut"] = True
        bundle, _ = run(dump)
        self.assertFalse(bundle.rooms_sold_out)
        self.assertEqual(len(bundle.offers), 1)


class RuleTable(unittest.TestCase):
    def test_every_rule_has_valid_layer_and_severity(self):
        for rule, (layer, severity, text) in RULES.items():
            self.assertIn(layer, (1, 2, 3, 4), rule)
            self.assertIn(severity, ("error", "warning", "fixed"), rule)
            self.assertTrue(text, rule)
        self.assertLess(GATE["max_reject_ratio"], 1)


if __name__ == "__main__":
    unittest.main()
