const state = {
  rows: [], total: 0, limit: 20, offset: 0, search: "", locale: "vi", currency: "VND",
  star: "", status: "", sort: "recent", city: "", hotelId: null, detail: null, sections: {}, requestId: 0, soAnh: 60,
};

const $ = (selector) => document.querySelector(selector);
const els = {
  catalog: $("#catalog-view"), detail: $("#detail-view"), list: $("#hotel-list"),
  loading: $("#catalog-loading"), empty: $("#catalog-empty"), error: $("#catalog-error"),
  summary: $("#catalog-summary"), total: $("#metric-total"), range: $("#catalog-range"),
  page: $("#catalog-page"), prev: $("#catalog-prev"), next: $("#catalog-next"),
  search: $("#hotel-search"), locale: $("#catalog-locale"), currency: $("#catalog-currency"),
  star: $("#star-filter"), status: $("#status-filter"), sort: $("#sort-filter"),
  city: $("#city-filter"), heroTitle: $("#hero-title"),
  dbStatus: $("#db-status"), detailLocale: $("#detail-locale"),
  detailBody: $("#detail-body"), tripLink: $("#trip-link"), toast: $("#toast"),
};

const fmt = (value) => new Intl.NumberFormat("vi-VN").format(Number(value || 0));
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const text = (value, fallback = "Chưa có dữ liệu") => value === null || value === undefined || value === "" ? fallback : String(value);
const safeUrl = (value) => { try { const url = new URL(value); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } };
const valueText = (value) => typeof value === "object" && value !== null ? JSON.stringify(value) : text(value);

async function api(path) {
  const response = await fetch(path, {headers:{Accept:"application/json"}});
  const payload = await response.json().catch(() => ({error:`HTTP ${response.status}`}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function showToast(message) {
  els.toast.textContent = message; els.toast.classList.add("show"); clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.remove("show"), 2800);
}

async function loadHealth() {
  try {
    const result = await api("/api/health");
    els.dbStatus.className = "connection online"; els.dbStatus.innerHTML = `<i></i><span>${esc(result.database)} · Đã kết nối</span>`;
  } catch {
    els.dbStatus.className = "connection error"; els.dbStatus.innerHTML = "<i></i><span>Mất kết nối</span>";
  }
}

function catalogueParams() {
  const params = new URLSearchParams({limit:state.limit, offset:state.offset, locale:state.locale, currency:state.currency, sort:state.sort});
  if (state.search) params.set("search", state.search);
  if (state.star) params.set("star", state.star);
  if (state.status) params.set("status", state.status);
  if (state.city) params.set("city", state.city);
  return params;
}

async function loadCities() {
  // Thành phố nào có trong DB thì hiện thành phố đó — không gắn cứng TP.HCM nữa.
  try {
    const payload = await api("/api/cities");
    const chon = state.city;
    els.city.replaceChildren(new Option(`Tất cả thành phố (${fmt(payload.total)})`, ""));
    for (const city of payload.cities || []) {
      const ten = state.locale === "en" ? (city.name_en || city.name_vi) : (city.name_vi || city.name_en);
      const qg = state.locale === "en" ? (city.country_en || city.country_vi) : (city.country_vi || city.country_en);
      const nhan = `${ten}${qg ? " · " + qg : ""} (${fmt(city.hotel_count)})`;
      els.city.appendChild(new Option(nhan, String(city.trip_city_id)));
    }
    els.city.value = chon;
    capNhatTieuDe();
  } catch (error) {
    els.city.replaceChildren(new Option("Không tải được danh sách thành phố", ""));
  }
}

function capNhatTieuDe() {
  // Tiêu đề chạy theo thành phố đang chọn, không gắn cứng tên thành phố nào.
  if (!els.heroTitle) return;
  const chon = els.city.selectedOptions[0];
  els.heroTitle.textContent = state.city && chon
    ? `Khách sạn tại ${chon.textContent.replace(/\s*\(\d[\d.,]*\)\s*$/, "")}`
    : "Khách sạn đã thu thập";
}

async function loadHotels() {
  const requestId = ++state.requestId;
  els.loading.classList.remove("hidden"); els.empty.classList.add("hidden"); els.error.classList.add("hidden");
  try {
    const payload = await api(`/api/hotels?${catalogueParams()}`);
    if (requestId !== state.requestId) return;
    state.rows = payload.rows; state.total = payload.total;
    renderHotels(); renderPagination();
  } catch (error) {
    if (requestId !== state.requestId) return;
    state.rows = []; els.list.replaceChildren(); els.error.textContent = error.message; els.error.classList.remove("hidden");
  } finally {
    if (requestId === state.requestId) els.loading.classList.add("hidden");
  }
}

function statusLabel(status) {
  return {complete:"Đủ detail", partial:"Thiếu một phần", missing:"Chưa có detail"}[status] || status;
}

function priceLabel(row) {
  if (row.min_price === null || row.min_price === undefined) return `<div class="price"><small>Giá thấp nhất</small><strong>Chưa có</strong></div>`;
  const options = row.currency === "VND" ? {maximumFractionDigits:0} : {minimumFractionDigits:0, maximumFractionDigits:2};
  return `<div class="price"><small>Giá thấp nhất</small><strong>${esc(new Intl.NumberFormat("vi-VN", options).format(Number(row.min_price)))} ${esc(row.currency)}</strong></div>`;
}

function renderHotels() {
  els.list.replaceChildren();
  for (const hotel of state.rows) {
    const card = document.createElement("article"); card.className = "hotel-card"; card.tabIndex = 0;
    const image = safeUrl(hotel.image_url);
    const stars = hotel.star_rating ? "★".repeat(Math.max(0, Math.min(5, Number(hotel.star_rating)))) : "Chưa xếp hạng";
    card.innerHTML = `
      <div class="hotel-thumb">
        ${image ? `<img src="${esc(image)}" alt="${esc(text(hotel.name, "Ảnh khách sạn"))}" loading="lazy">` : '<div class="no-image">Chưa có ảnh</div>'}
        <span class="status-pill ${esc(hotel.detail_status)}">${esc(statusLabel(hotel.detail_status))}</span>
      </div>
      <div class="hotel-info">
        <div class="hotel-id">TRIP ID · ${esc(hotel.trip_hotel_id)}</div>
        <h3>${esc(text(hotel.name, `Khách sạn #${hotel.trip_hotel_id}`))}</h3>
        <p class="hotel-address">${esc(text(hotel.address))}</p>
        <div class="rating-row"><span class="stars">${esc(stars)}</span>${hotel.review_score !== null ? `<span class="score">${esc(hotel.review_score)}</span>` : ""}<span class="reviews">${fmt(hotel.review_count)} đánh giá</span></div>
        <div class="hotel-data-counts"><span>▧ ${fmt(hotel.image_count)} ảnh</span><span>✓ ${fmt(hotel.amenity_count)} tiện nghi</span><span>▤ ${fmt(hotel.room_count)} phòng</span><span>⌖ ${fmt(hotel.nearby_count)} địa điểm</span></div>
        <div class="hotel-bottom"><div class="hotel-type">${esc(text(hotel.hotel_type, "Chưa xác định loại hình"))}</div>${priceLabel(hotel)}</div>
      </div>`;
    const open = () => openHotel(hotel.id);
    card.addEventListener("click", open); card.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); open(); } });
    const imageElement = card.querySelector("img");
    if (imageElement) imageElement.addEventListener("error", () => { imageElement.replaceWith(Object.assign(document.createElement("div"), {className:"no-image", textContent:"Không tải được ảnh"})); });
    els.list.append(card);
  }
  els.empty.classList.toggle("hidden", state.rows.length !== 0);
}

function renderPagination() {
  const start = state.total ? state.offset + 1 : 0; const end = Math.min(state.offset + state.limit, state.total);
  els.total.textContent = fmt(state.total); els.summary.textContent = `${fmt(state.total)} khách sạn phù hợp · Hiển thị ${fmt(start)}–${fmt(end)}`;
  els.range.textContent = `${fmt(start)}–${fmt(end)} / ${fmt(state.total)}`; els.page.textContent = Math.floor(state.offset / state.limit) + 1;
  els.prev.disabled = state.offset === 0; els.next.disabled = state.offset + state.limit >= state.total;
}

/* ---------------------------------------------------------------------------
   Trang chi tiết dựng theo bố cục trang khách sạn của Trip.com (một trang cuộn
   liền mạch) để đối chiếu dữ liệu crawl với trang gốc cho nhanh.
   Bố cục tự dựng, không dùng logo / nhận diện thương hiệu của Trip.com.
--------------------------------------------------------------------------- */
const TP_SECTIONS = ["images", "rooms", "prices", "policies", "nearby", "amenities"];

async function openHotel(id, push = true) {
  state.hotelId = Number(id);
  state.sections = {};
  state.soAnh = ANH_MOI_LAN;
  els.catalog.classList.add("hidden");
  els.detail.classList.remove("hidden");
  els.detailLocale.value = state.locale;
  els.detailBody.innerHTML = '<div class="detail-loading">Đang tải dữ liệu khách sạn...</div>';
  window.scrollTo({top: 0, behavior: "instant"});
  if (push) { const url = new URL(location.href); url.searchParams.set("hotel", id); history.pushState({hotel: id}, "", url); }
  const requestId = ++state.requestId;
  try {
    const results = await Promise.all([
      api(`/api/hotels/${id}?locale=${state.locale}`),
      ...TP_SECTIONS.map((name) => api(`/api/hotels/${id}/${name}?locale=${state.locale}`).catch(() => ({items: []}))),
    ]);
    if (requestId !== state.requestId) return;
    state.detail = results[0];
    TP_SECTIONS.forEach((name, index) => { state.sections[name] = results[index + 1].items || []; });
    renderTripDetail();
  } catch (error) {
    els.detailBody.innerHTML = `<div class="error-box">${esc(error.message)}</div>`;
  }
}

function closeHotel(push = true) {
  state.hotelId = null; state.detail = null; state.sections = {}; state.requestId += 1;
  els.detail.classList.add("hidden"); els.catalog.classList.remove("hidden");
  if (push) { const url = new URL(location.href); url.searchParams.delete("hotel"); history.pushState({}, "", url); }
  window.scrollTo({top: 0, behavior: "instant"});
}

/* ------------------------------ tiện ích nhỏ ------------------------------ */
const money = (value, currency) => value === null || value === undefined
  ? "—"
  : `${new Intl.NumberFormat("vi-VN", {maximumFractionDigits: 0}).format(Number(value))} ${esc(currency || "")}`.trim();

function khoangCach(item) {
  if (item.distance_km !== null && item.distance_km !== undefined) {
    const km = Number(item.distance_km);
    if (Number.isFinite(km)) {
      return km < 1
        ? `${Math.round(km * 1000)} m`
        : `${km.toFixed(km >= 10 ? 0 : 1).replace(".", ",")} km`;
    }
  }
  const raw = String(item.distance_text || "").trim();
  const compact = raw.match(/(\d+(?:[.,]\d+)?)\s*(m|km)\b/i);
  return compact ? `${compact[1]} ${compact[2].toLowerCase()}` : raw;
}

// Giờ nhận / trả phòng lấy từ mục chính sách checkInAndOut của Trip.com.
function gioNhanTra(policies) {
  const section = policies.find((row) => /checkin|check_in|nhan/i.test(String(row.policy_code || "")));
  if (!section) return {};
  const lines = String(section.description || "").split("\n");
  const pick = (pattern) => {
    const line = lines.find((one) => pattern.test(one));
    const found = line && line.match(/\d{1,2}[:h]\d{2}/);
    return found ? found[0] : null;
  };
  return {vao: pick(/nhận phòng|check[\s-]?in/i), ra: pick(/trả phòng|check[\s-]?out/i)};
}

/* ------------------------------ các khối ---------------------------------- */
function tpGallery(images) {
  if (!images.length) return '<div class="tp-empty">Chưa crawl được ảnh khách sạn.</div>';
  const shown = images.slice(0, 5);
  const extra = images.length - shown.length;
  return `<div class="tp-gallery">${shown.map((image, index) => {
    const url = safeUrl(image.url);
    const caption = text(image.category_name || image.source_category, "");
    return `<figure class="${index === 0 ? "main" : ""}">${url ? `<img src="${esc(url)}" loading="lazy" alt="${esc(caption || "Ảnh khách sạn")}">` : ""}${caption ? `<span class="cap">${esc(caption)}</span>` : ""}</figure>`;
  }).join("")}<button class="tp-gallery-more" data-goto="tp-images">▧ Xem tất cả ${fmt(images.length)} ảnh${extra > 0 ? ` · +${fmt(extra)}` : ""}</button></div>`;
}

function tpHead(hotel) {
  const stars = hotel.star_rating ? "★".repeat(Math.max(0, Math.min(5, Math.round(Number(hotel.star_rating))))) : "";
  const noiChon = [text(hotel.location_name, ""), text(hotel.country_name, "")].filter(Boolean).join(", ");
  const toaDo = hotel.latitude && hotel.longitude ? `<span class="coord">${esc(hotel.latitude)}, ${esc(hotel.longitude)}</span>` : "";
  const diem = hotel.review_score === null || hotel.review_score === undefined ? "" : `
    <div class="tp-score"><div class="txt"><b>${Number(hotel.review_score) >= 9 ? "Tuyệt vời" : Number(hotel.review_score) >= 8 ? "Rất tốt" : "Điểm đánh giá"}</b><span>${fmt(hotel.review_count)} đánh giá</span></div>
      <div class="num">${esc(hotel.review_score)}<small>/10</small></div></div>`;
  return `<div class="tp-head"><div class="tp-head-main">
      <div class="tp-kicker">${esc(text(hotel.hotel_type, "Khách sạn"))} · Trip ID ${esc(hotel.trip_hotel_id)}</div>
      <h1>${esc(text(hotel.name, `Khách sạn #${hotel.trip_hotel_id}`))} ${stars ? `<span class="tp-stars" aria-label="${esc(hotel.star_rating)} sao">${stars}</span>` : ""}</h1>
      ${hotel.local_name ? `<p class="tp-localname">${esc(hotel.local_name)}</p>` : ""}
      <p class="tp-addr"><span class="tp-pin">●</span>${esc(text(hotel.address, "Chưa có địa chỉ"))}${noiChon ? ` · ${esc(noiChon)}` : ""}${toaDo}</p>
    </div>${diem}</div>`;
}

function tpFacts(hotel, policies) {
  const gio = gioNhanTra(policies);
  const o = [];
  const them = (nhan, giaTri) => { if (giaTri !== null && giaTri !== undefined && giaTri !== "") o.push(`<div>${esc(nhan)} <b>${esc(giaTri)}</b></div>`); };
  them("Trip hotel ID", hotel.trip_hotel_id);
  them("Loại hình", hotel.hotel_type);
  them("Hạng sao", hotel.star_rating ? `${hotel.star_rating}${hotel.star_type ? ` (${hotel.star_type})` : ""}` : "");
  them("Số phòng", hotel.hotel_room_count ? fmt(hotel.hotel_room_count) : "");
  them("Khai trương", hotel.open_year);
  them("Sửa chữa", hotel.renovated_year);
  them("Nhận phòng", gio.vao);
  them("Trả phòng", gio.ra);
  them("Trạng thái detail", statusLabel(hotel.detail_status));
  return `<div class="tp-facts">${o.join("")}</div>`;
}

function tpNav(muc) {
  return `<nav class="tp-nav" id="tp-nav">${muc.map((one, index) =>
    `<button data-goto="${esc(one.id)}"${index === 0 ? ' class="active"' : ""}>${esc(one.ten)}</button>`).join("")}</nav>`;
}

function tpAbout(hotel) {
  const phu = [
    hotel.zone_name ? `Khu vực: ${hotel.zone_name}` : "",
    hotel.traffic_desc ? `Giao thông: ${hotel.traffic_desc}` : "",
  ].filter(Boolean).join(" · ");
  return `<div class="tp-about">
      <h2>Thông tin nổi bật</h2>
      ${phu ? `<div class="labels">${esc(phu)}</div>` : ""}
      <div class="body">${esc(text(hotel.description, "Chưa crawl được phần mô tả."))}</div>
    </div>`;
}

function giaThapNhat(prices) {
  const hopLe = prices.filter((one) => !one.is_sold_out && one.price !== null && one.price !== undefined && Number.isFinite(Number(one.price)));
  return hopLe.sort((a, b) => Number(a.price) - Number(b.price))[0] || null;
}

function tpBookingSummary(prices, hotelUrl) {
  const gia = giaThapNhat(prices);
  const link = safeUrl(hotelUrl);
  return `<aside class="tp-booking-card">
    <div class="tp-booking-label">Giá tham khảo thấp nhất</div>
    ${gia ? `<div class="tp-booking-price">${money(gia.price, gia.currency)}<small>/ phòng / đêm</small></div>
      <div class="tp-booking-date">${esc(text(gia.check_in, "Ngày nhận phòng"))} → ${esc(text(gia.check_out, "Ngày trả phòng"))}</div>`
      : '<div class="tp-booking-missing">Chưa có giá động cho ngày đang chọn</div>'}
    <button class="tp-primary" data-goto="tp-rooms">Xem phòng trống</button>
    ${link ? `<a class="tp-source-link" href="${esc(link)}" target="_blank" rel="noopener">Mở khách sạn gốc ↗</a>` : ""}
    <p>Dữ liệu hiển thị từ lần crawl gần nhất; giá thực tế có thể thay đổi.</p>
  </aside>`;
}

function tpOffers(offers, hotelUrl) {
  const link = safeUrl(hotelUrl);
  if (!offers.length) return `<div class="tp-no-rate"><span>Chưa có gói giá động cho phòng này.</span>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener">Kiểm tra phòng ↗</a>` : ""}</div>`;
  return `<table class="tp-offers"><thead><tr>
      <th>Lựa chọn phòng</th><th>Quyền lợi</th><th>Sức chứa</th><th class="right">Giá hôm nay</th><th></th>
    </tr></thead><tbody>${offers.map((offer) => {
    const tags = [
      offer.breakfast_included ? '<span class="tp-tag ok">Có bữa sáng</span>' : '<span class="tp-tag no">Không bữa sáng</span>',
      offer.free_cancellation ? '<span class="tp-tag ok">Hủy miễn phí</span>' : '<span class="tp-tag no">Không hoàn hủy</span>',
      offer.is_sold_out ? '<span class="tp-tag sold">Hết phòng</span>' : "",
    ].join("");
    const them = [
      offer.total_price !== null && offer.total_price !== undefined ? `Tổng ${money(offer.total_price, offer.currency)}` : "",
      offer.taxes_fees !== null && offer.taxes_fees !== undefined ? `thuế/phí ${money(offer.taxes_fees, offer.currency)}` : "",
    ].filter(Boolean).join(" · ");
    return `<tr>
        <td><b>${esc(text(offer.price_type, `Gói #${offer.id}`))}</b><small>${esc(text(offer.check_in, "—"))} → ${esc(text(offer.check_out, "—"))}</small></td>
        <td>${tags}</td>
        <td><span class="tp-guests">●●</span><small>Ghi nhận ${esc(text(offer.captured_date, "—"))}</small></td>
        <td class="price">${money(offer.price, offer.currency)}${them ? `<small>${esc(them)}</small>` : ""}</td>
        <td class="action">${offer.is_sold_out ? '<button disabled>Hết phòng</button>' : link ? `<a href="${esc(link)}" target="_blank" rel="noopener">Chọn</a>` : '<button disabled>Chọn</button>'}</td>
      </tr>`;
  }).join("")}</tbody></table>`;
}

function tpRooms(rooms, prices, hotelUrl) {
  if (!rooms.length) return '<div class="tp-empty">Chưa crawl được dữ liệu phòng.</div>';
  const theoPhong = new Map();
  for (const price of prices) {
    if (!theoPhong.has(price.room_type_id)) theoPhong.set(price.room_type_id, []);
    theoPhong.get(price.room_type_id).push(price);
  }
  return rooms.map((room) => {
    const anh = safeUrl(room.images?.[0]?.url);
    const tienNghi = (room.amenities || []).map((one) => text(one.amenity_name, "")).filter(Boolean);
    const meta = [
      text(room.bed_type, room.bed_count ? `${room.bed_count} giường` : ""),
      room.area_text || (room.area_sqm ? `${room.area_sqm} m²` : ""),
      room.guest_text || (room.max_occupancy ? `Tối đa ${room.max_occupancy} khách` : ""),
      room.bedroom_count ? `${room.bedroom_count} phòng ngủ` : "",
      room.bathroom_count ? `${room.bathroom_count} phòng tắm` : "",
      room.view_name || "",
      room.smoking_policy || "",
      room.wifi ? "Wi-Fi" : "",
      room.extra_bed_policy || "",
    ].filter(Boolean);
    return `<article class="tp-room">
        <div class="tp-room-info">
          <div>${anh ? `<img src="${esc(anh)}" loading="lazy" alt="${esc(text(room.name, "Phòng"))}">` : '<div class="tp-room-noimg">Chưa có ảnh phòng</div>'}
            ${room.images?.length ? `<div class="tp-room-photo-count">▧ ${fmt(room.images.length)} ảnh</div>` : ""}</div>
          <div>
            <h3>${esc(text(room.name, `Phòng #${room.trip_room_id || room.id}`))}</h3>
            <div class="tp-room-meta">${meta.map((one) => `<span>${esc(one)}</span>`).join("")}</div>
            ${tienNghi.length ? `<div class="tp-room-am">${tienNghi.slice(0, 8).map((one) => `<span>✓ ${esc(one)}</span>`).join("")}${tienNghi.length > 8 ? `<span class="more-am">+${fmt(tienNghi.length - 8)} tiện nghi</span>` : ""}</div>` : '<div class="tp-room-am muted">Chưa có tiện nghi phòng.</div>'}
          </div>
        </div>
        <div class="tp-room-rates">${tpOffers(theoPhong.get(room.id) || [], hotelUrl)}</div>
      </article>`;
  }).join("");
}

function tpAmenities(items) {
  if (!items.length) return '<div class="tp-empty">Chưa crawl được tiện nghi.</div>';
  const nhom = new Map();
  for (const item of items) {
    const key = text(item.category, "Khác");
    if (!nhom.has(key)) nhom.set(key, []);
    nhom.get(key).push(item);
  }
  return [...nhom].map(([ten, ds]) => `<div class="tp-am-group">
      <h3>${esc(ten)} <small>(${fmt(ds.length)})</small></h3>
      <div class="tp-chips">${ds.map((one) => {
    const lop = ["tp-chip", one.is_highlight ? "hi" : "", one.is_available === false ? "off" : ""].filter(Boolean).join(" ");
    const phu = one.fee_label || one.free_type || "";
    return `<span class="${lop}"${one.is_available === false ? ' title="Không được cung cấp"' : ""}><i>${one.is_available === false ? "×" : "✓"}</i>${esc(text(one.amenity_name, "—"))}${phu ? `<em>${esc(phu)}</em>` : ""}</span>`;
  }).join("")}</div></div>`).join("");
}

function policyText(value) {
  const holder = document.createElement("div");
  holder.innerHTML = String(value ?? "").replace(/<\/(p|li)>/gi, "\n").replace(/<br\s*\/?\s*>/gi, "\n");
  return holder.textContent.replace(/\n\s*\n+/g, "\n").trim();
}

function tpPolicies(items) {
  if (!items.length) return '<div class="tp-empty">Chưa crawl được chính sách.</div>';
  return items.map((one) => `<div class="tp-policy">
      <h3>${esc(text(one.title, one.policy_code))}<span class="code">${esc(text(one.policy_code, ""))}</span></h3>
      <div class="body">${esc(text(policyText(one.description), "Không có nội dung."))}</div>
    </div>`).join("");
}

function tpNearby(items) {
  if (!items.length) return '<div class="tp-empty">Chưa crawl được địa điểm lân cận.</div>';
  const nhom = new Map();
  for (const item of items) {
    const key = text(item.category_name || item.category_code, "Khác");
    if (!nhom.has(key)) nhom.set(key, []);
    nhom.get(key).push(item);
  }

  const iconFor = (name) => {
    const value = String(name || "").toLowerCase();
    if (/transport|giao thông|ga tàu|metro/.test(value)) return "&#128646;";
    if (/shopping|mua sắm|shop/.test(value)) return "&#128717;";
    return "&#9679;";
  };
  const renderRow = (one) => {
    const name = text(one.name, "—");
    const note = text(one.description, "");
    return `<li>
      <span class="tp-place"><b title="${esc(name)}">${esc(name)}</b>${note ? `<span class="geo" title="${esc(note)}">${esc(note)}</span>` : ""}</span>
      <span class="km">${esc(khoangCach(one))}</span>
    </li>`;
  };

  return `<div class="tp-loc">${[...nhom].map(([ten, ds]) => {
    ds.sort((a, b) => (a.distance_km ?? 9999) - (b.distance_km ?? 9999));
    const visible = ds.slice(0, 6);
    const hidden = ds.slice(6);
    return `<section class="tp-loc-card">
      <header class="tp-loc-head">
        <span class="tp-loc-icon" aria-hidden="true">${iconFor(ten)}</span>
        <div><h3>${esc(ten)}</h3><p>${fmt(ds.length)} địa điểm gần khách sạn</p></div>
      </header>
      <ul>${visible.map(renderRow).join("")}</ul>
      ${hidden.length ? `<details class="tp-loc-details"><summary>Xem thêm ${fmt(hidden.length)} địa điểm <span aria-hidden="true">⌄</span></summary><ul>${hidden.map(renderRow).join("")}</ul></details>` : ""}
    </section>`;
  }).join("")}</div>`;
}

const ANH_MOI_LAN = 60;

function tpImages(items, gioiHan = ANH_MOI_LAN) {
  if (!items.length) return '<div class="tp-empty">Chưa crawl được ảnh.</div>';
  const hien = items.slice(0, gioiHan);
  const conLai = items.length - hien.length;
  return `<div class="image-grid">${hien.map((one) => {
    const url = safeUrl(one.url);
    return `<figure class="image-card">${url ? `<img src="${esc(url)}" loading="lazy" alt="Ảnh khách sạn">` : ""}<span>${esc(text(one.category_name || one.source_category, "Khác"))}</span></figure>`;
  }).join("")}</div>${conLai > 0 ? `<button class="ghost-button" id="tp-them-anh" style="margin-top:12px">Hiện thêm ${fmt(Math.min(conLai, ANH_MOI_LAN))} ảnh (còn ${fmt(conLai)})</button>` : ""}`;
}

function tpTranslations(rows) {
  if (!rows.length) return '<div class="tp-empty">Chưa có bản dịch nào.</div>';
  return `<div class="table-wrap"><table class="simple-table"><thead><tr>
      <th>Ngôn ngữ</th><th>Tên</th><th>Tên bản địa</th><th>Địa chỉ</th><th>Loại hình</th><th>Mô tả</th>
    </tr></thead><tbody>${rows.map((row) => `<tr>
      <td>${esc(row.locale)}</td><td>${esc(text(row.name, "—"))}</td><td>${esc(text(row.local_name, "—"))}</td>
      <td>${esc(text(row.address, "—"))}</td><td>${esc(text(row.hotel_type, "—"))}</td>
      <td>${esc(String(text(row.description, "—")).slice(0, 300))}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

/* ------------------------------ lắp trang --------------------------------- */
function renderTripDetail() {
  const hotel = state.detail.hotel;
  const s = state.sections;
  els.tripLink.href = safeUrl(hotel.url) || "#";

  const muc = [
    {id: "tp-about", ten: "Tổng quan"},
    {id: "tp-rooms", ten: `Phòng & giá (${fmt(s.rooms.length)})`},
    {id: "tp-amenities", ten: `Tiện nghi (${fmt(s.amenities.length)})`},
    {id: "tp-policies", ten: `Chính sách (${fmt(s.policies.length)})`},
    {id: "tp-nearby", ten: `Vị trí lân cận (${fmt(s.nearby.length)})`},
    {id: "tp-images", ten: `Ảnh (${fmt(s.images.length)})`},
    {id: "tp-data", ten: "Dữ liệu thô"},
  ];

  els.detailBody.innerHTML = `
    ${tpHead(hotel)}
    ${tpGallery(s.images)}
    ${tpNav(muc)}
    <section class="tp-block tp-overview" id="tp-about">
      <div class="tp-overview-main">${tpFacts(hotel, s.policies)}${tpAbout(hotel)}</div>
      ${tpBookingSummary(s.prices, hotel.url)}
    </section>
    <section class="tp-block" id="tp-rooms"><div class="tp-section-title"><div><span>Phòng nghỉ</span><h2>Chọn phòng phù hợp</h2></div><small>${fmt(s.rooms.length)} loại phòng · ${fmt(s.prices.length)} mức giá</small></div>${tpRooms(s.rooms, s.prices, hotel.url)}</section>
    <section class="tp-block tp-surface" id="tp-amenities"><h2>Tiện nghi &amp; dịch vụ</h2>${tpAmenities(s.amenities)}</section>
    <section class="tp-block tp-surface" id="tp-policies"><h2>Chính sách khách sạn</h2>${tpPolicies(s.policies)}</section>
    <section class="tp-block tp-surface" id="tp-nearby"><h2>Vị trí &amp; địa điểm lân cận</h2>${tpNearby(s.nearby)}</section>
    <section class="tp-block tp-surface" id="tp-images"><h2>Thư viện ảnh <small>(${fmt(s.images.length)})</small></h2><div id="tp-anh">${tpImages(s.images, state.soAnh)}</div></section>
    <section class="tp-block" id="tp-data"><h2>Đối chiếu dữ liệu đã lưu</h2>
      <h3 style="font-size:15px;margin:0 0 8px">Bản dịch VI / EN</h3>
      ${tpTranslations(state.detail.translations)}
      <details style="margin-top:16px"><summary style="cursor:pointer;font-weight:600">Xem JSON bản ghi khách sạn</summary>
        <pre class="raw-view">${esc(JSON.stringify({hotel, translations: state.detail.translations}, null, 2))}</pre>
      </details>
    </section>`;

  const khungAnh = document.querySelector("#tp-anh");
  khungAnh?.addEventListener("click", (event) => {
    if (!event.target.closest("#tp-them-anh")) return;
    state.soAnh += ANH_MOI_LAN;
    khungAnh.innerHTML = tpImages(state.sections.images, state.soAnh);
  });

  const nav = document.querySelector("#tp-nav");
  for (const button of els.detailBody.querySelectorAll("[data-goto]")) button.addEventListener("click", () => {
    document.querySelector(`#${button.dataset.goto}`)?.scrollIntoView({behavior: "smooth", block: "start"});
  });
  theoDoiCuon(nav, muc);
}

// Tô sáng mục đang xem khi cuộn trang.
function theoDoiCuon(nav, muc) {
  if (typeof IntersectionObserver !== "function") return;
  const doi = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      for (const button of nav.querySelectorAll("button")) {
        button.classList.toggle("active", button.dataset.goto === entry.target.id);
      }
    }
  }, {rootMargin: "-56px 0px -70% 0px", threshold: 0});
  for (const one of muc) {
    const node = document.querySelector(`#${one.id}`);
    if (node) doi.observe(node);
  }
}

let searchTimer;
els.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer=setTimeout(() => {state.search=els.search.value.trim(); state.offset=0; loadHotels();},350); });
for (const [element,key] of [[els.currency,"currency"],[els.star,"star"],[els.status,"status"],[els.sort,"sort"],[els.city,"city"]]) element.addEventListener("change", () => {state[key]=element.value; state.offset=0; capNhatTieuDe(); loadHotels();});
// Đổi ngôn ngữ thì nạp lại tên thành phố theo đúng thứ tiếng đó.
els.locale.addEventListener("change", () => {state.locale=els.locale.value; state.offset=0; loadCities(); loadHotels();});
els.prev.addEventListener("click", () => {state.offset=Math.max(0,state.offset-state.limit); loadHotels(); window.scrollTo({top:0,behavior:"smooth"});});
els.next.addEventListener("click", () => {state.offset+=state.limit; loadHotels(); window.scrollTo({top:0,behavior:"smooth"});});
$("#refresh-hotels").addEventListener("click", () => {loadHealth(); loadCities(); loadHotels(); showToast("Đang làm mới dữ liệu từ PostgreSQL.");});
$("#back-to-list").addEventListener("click", () => closeHotel());
els.detailLocale.addEventListener("change", async () => {state.locale=els.detailLocale.value; els.locale.value=state.locale; state.sections={}; await openHotel(state.hotelId,false);});
window.addEventListener("popstate", () => {const id=new URLSearchParams(location.search).get("hotel"); if (id) openHotel(id,false); else closeHotel(false);});

async function init() {
  await Promise.all([loadHealth(), loadCities(), loadHotels()]);
  const id = new URLSearchParams(location.search).get("hotel"); if (id) await openHotel(id,false);
}
init().catch((error) => {els.error.textContent=error.message; els.error.classList.remove("hidden");});
