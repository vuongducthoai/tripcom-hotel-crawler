const state = {
  rows: [], total: 0, limit: 20, offset: 0, search: "", locale: "vi", currency: "VND",
  star: "", status: "", sort: "recent", hotelId: null, detail: null, sections: {}, requestId: 0,
};

const $ = (selector) => document.querySelector(selector);
const els = {
  catalog: $("#catalog-view"), detail: $("#detail-view"), list: $("#hotel-list"),
  loading: $("#catalog-loading"), empty: $("#catalog-empty"), error: $("#catalog-error"),
  summary: $("#catalog-summary"), total: $("#metric-total"), range: $("#catalog-range"),
  page: $("#catalog-page"), prev: $("#catalog-prev"), next: $("#catalog-next"),
  search: $("#hotel-search"), locale: $("#catalog-locale"), currency: $("#catalog-currency"),
  star: $("#star-filter"), status: $("#status-filter"), sort: $("#sort-filter"),
  dbStatus: $("#db-status"), detailLocale: $("#detail-locale"),
  detailHero: $("#hotel-overview"), detailContent: $("#detail-content"), tripLink: $("#trip-link"),
  tabs: $("#detail-tabs"), toast: $("#toast"),
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
  return params;
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

async function openHotel(id, push = true) {
  state.hotelId = Number(id); state.sections = {}; els.catalog.classList.add("hidden"); els.detail.classList.remove("hidden");
  els.detailLocale.value = state.locale;
  els.detailHero.innerHTML = '<div class="detail-loading">Đang tải thông tin khách sạn...</div>'; els.detailContent.innerHTML = '<div class="detail-loading">Đang tải dữ liệu...</div>';
  window.scrollTo({top:0, behavior:"instant"});
  if (push) { const url = new URL(location.href); url.searchParams.set("hotel", id); history.pushState({hotel:id}, "", url); }
  try {
    state.detail = await api(`/api/hotels/${id}?locale=${state.locale}`);
    renderDetailHero(); await activateSection("overview");
  } catch (error) {
    els.detailHero.innerHTML = `<div class="error-box">${esc(error.message)}</div>`; els.detailContent.replaceChildren();
  }
}

function closeHotel(push = true) {
  state.hotelId = null; state.detail = null; state.sections = {}; els.detail.classList.add("hidden"); els.catalog.classList.remove("hidden");
  if (push) { const url = new URL(location.href); url.searchParams.delete("hotel"); history.pushState({}, "", url); }
  window.scrollTo({top:0, behavior:"instant"});
}

function renderDetailHero() {
  const h = state.detail.hotel; const stars = h.star_rating ? "★".repeat(Math.min(5, Number(h.star_rating))) : "Chưa xếp hạng";
  els.tripLink.href = safeUrl(h.url) || "#";
  els.detailHero.innerHTML = `
    <div class="detail-title"><div class="eyebrow">TRIP HOTEL ID · ${esc(h.trip_hotel_id)}</div><h1>${esc(text(h.name, `Khách sạn #${h.trip_hotel_id}`))}</h1><p>${esc(text(h.address))}</p>
      <div class="detail-badges"><span>${esc(stars)}</span><span>${esc(text(h.hotel_type, "Chưa xác định loại hình"))}</span><span>${esc(text(h.location_name, "TP. Hồ Chí Minh"))}</span><span>${esc(statusLabel(h.detail_status))}</span></div>
    </div>
    <div><div class="detail-score"><div><strong>Điểm đánh giá</strong><span>${fmt(h.review_count)} lượt đánh giá</span></div><div class="score-large">${esc(h.review_score ?? "—")}</div></div>
      <div class="detail-counts"><div><b>${fmt(h.image_count)}</b><small>ẢNH</small></div><div><b>${fmt(h.amenity_count)}</b><small>TIỆN NGHI</small></div><div><b>${fmt(h.room_count)}</b><small>LOẠI PHÒNG</small></div><div><b>${fmt(h.price_count)}</b><small>MỨC GIÁ</small></div><div><b>${fmt(h.policy_count)}</b><small>CHÍNH SÁCH</small></div><div><b>${fmt(h.nearby_count)}</b><small>LÂN CẬN</small></div></div>
    </div>`;
}

async function activateSection(section) {
  document.querySelectorAll("#detail-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.section === section));
  els.detailContent.innerHTML = '<div class="detail-loading"><span class="page-loading"><span></span></span>Đang tải phân mục...</div>';
  try {
    if (section === "overview") return renderOverview();
    if (section === "translations") return renderTranslations();
    if (!state.sections[section]) state.sections[section] = await api(`/api/hotels/${state.hotelId}/${section}?locale=${state.locale}`);
    renderSection(section, state.sections[section]);
  } catch (error) { els.detailContent.innerHTML = `<div class="error-box">${esc(error.message)}</div>`; }
}

function heading(title, subtitle, count = null) {
  return `<div class="section-heading"><div><h2>${esc(title)}</h2><p>${esc(subtitle)}</p></div>${count === null ? "" : `<b>${fmt(count)} mục</b>`}</div>`;
}

function renderOverview() {
  const h = state.detail.hotel;
  els.detailContent.innerHTML = `${heading("Tổng quan khách sạn", "Thông tin chính đã chuẩn hóa từ dữ liệu crawl")}
    <div class="section-grid"><div class="info-card"><h3>Thông tin định danh</h3><div class="kv-grid">
      <div>Database ID</div><div>${esc(h.id)}</div><div>Trip hotel ID</div><div>${esc(h.trip_hotel_id)}</div><div>Loại hình</div><div>${esc(text(h.hotel_type))}</div><div>Hạng sao</div><div>${esc(text(h.star_rating))}</div><div>Điểm đánh giá</div><div>${esc(text(h.review_score))} / 10 (${fmt(h.review_count)} lượt)</div><div>Location ID</div><div>${esc(text(h.location_id))}</div>
    </div></div><div class="info-card"><h3>Vị trí và thời gian</h3><div class="kv-grid">
      <div>Khu vực</div><div>${esc(text(h.location_name))}</div><div>Quốc gia</div><div>${esc(text(h.country_code))}</div><div>Vĩ độ</div><div>${esc(text(h.latitude))}</div><div>Kinh độ</div><div>${esc(text(h.longitude))}</div><div>Ghi nhận đầu</div><div>${esc(text(h.first_seen_at))}</div><div>Cập nhật cuối</div><div>${esc(text(h.last_seen_at))}</div>
    </div></div></div><div class="info-card" style="margin-top:12px"><h3>Mô tả</h3><div class="description">${esc(text(h.description))}</div></div>`;
}

function renderTranslations() {
  const translations = state.detail.translations;
  els.detailContent.innerHTML = `${heading("Nội dung tiếng Việt / tiếng Anh", "Mỗi ngôn ngữ được lưu thành một bản dịch riêng", translations.length)}<div class="section-grid">${translations.map((item) => `<article class="translation-card"><header><h3>${esc(text(item.name))}</h3><span class="locale-pill">${esc(item.locale)}</span></header><address>${esc(text(item.address))}</address><div class="kv-grid"><div>Loại hình</div><div>${esc(text(item.hotel_type))}</div><div>Crawl lúc</div><div>${esc(text(item.crawled_at))}</div></div><p class="description">${esc(text(item.description))}</p></article>`).join("")}</div>`;
}

function renderSection(section, payload) {
  if (section === "images") return renderImages(payload.items);
  if (section === "amenities") {
    renderAmenities(payload.items);
    const unavailable = new Set(payload.items.filter(item => item.is_available === false).map(item => text(item.amenity_name)));
    for (const chip of els.detailContent.querySelectorAll('.amenity-chip')) {
      const nameNode = chip.firstChild;
      if (nameNode && unavailable.has(nameNode.textContent)) {
        const struck = document.createElement('s');
        struck.textContent = nameNode.textContent;
        chip.replaceChild(struck, nameNode);
        chip.title = 'Không được cung cấp';
      }
    }
    return;
  }
  if (section === "rooms") return renderRooms(payload.items);
  if (section === "prices") return renderPrices(payload.items);
  if (section === "policies") return renderPolicies(payload.items);
  if (section === "nearby") return renderNearby(payload.items);
  if (section === "raw") return renderRaw(payload);
}

function renderImages(items) {
  els.detailContent.innerHTML = `${heading("Thư viện ảnh", "Ảnh khách sạn được phân loại theo album Trip.com", items.length)}<div class="image-grid">${items.map((item) => { const url=safeUrl(item.url); return `<figure class="image-card">${url ? `<img src="${esc(url)}" alt="${esc(text(item.image_title, item.category_name || "Ảnh khách sạn"))}" loading="lazy">` : ""}<span>${esc(text(item.category_name || item.source_category, "Khác"))}</span></figure>`; }).join("")}</div>${items.length ? "" : '<div class="empty-view"><b>Chưa có ảnh</b></div>'}`;
}

function renderAmenities(items) {
  const groups = new Map();
  for (const item of items) { const category=text(item.category,"Khác"); if (!groups.has(category)) groups.set(category,[]); groups.get(category).push(item); }
  els.detailContent.innerHTML = `${heading("Tiện nghi & dịch vụ", "Các tiện nghi nổi bật, loại phí và thông tin bổ sung", items.length)}${[...groups].map(([category, values]) => `<div class="group"><h3 class="group-title">${esc(category)} <small>(${fmt(values.length)})</small></h3><div class="chip-list">${values.map((item) => `<span class="amenity-chip ${item.is_highlight ? "highlight" : ""}">${esc(text(item.amenity_name))}${item.free_type ? `<em>${esc(item.free_type)}</em>` : ""}${item.fee_label ? `<em>${esc(item.fee_label)}</em>` : ""}</span>`).join("")}</div></div>`).join("") || '<div class="empty-view"><b>Chưa có tiện nghi</b></div>'}`;
}

function renderRooms(items) {
  els.detailContent.innerHTML = `${heading("Phòng và tiện nghi phòng", "Tên phòng, giường, diện tích, sức chứa, ảnh và dịch vụ đi kèm", items.length)}<div class="room-list">${items.map((room) => {
    const mainImage=safeUrl(room.images?.[0]?.url); const amenities=(room.amenities || []).map((a) => text(a.amenity_name,a.amenity_key)).filter(Boolean);
    return `<article class="room-card"><div class="room-media">${mainImage ? `<img src="${esc(mainImage)}" alt="${esc(text(room.name,"Phòng"))}" loading="lazy">` : '<div class="room-no-image">Chưa có ảnh phòng</div>'}</div><div class="room-body"><h3>${esc(text(room.name,`Phòng #${room.trip_room_id || room.id}`))}</h3><div class="room-meta"><span>${esc(text(room.bed_type,"Chưa rõ giường"))}</span><span>${esc(text(room.area_sqm,"—"))} m²</span><span>Tối đa ${esc(text(room.max_occupancy,"—"))} người</span><span>${esc(text(room.bedroom_count,"—"))} phòng ngủ</span><span>${esc(text(room.bathroom_count,"—"))} phòng tắm</span>${room.view_name ? `<span>${esc(room.view_name)}</span>` : ""}${room.smoking_policy ? `<span>${esc(room.smoking_policy)}</span>` : ""}</div><div class="room-amenities"><b>${fmt(amenities.length)} tiện nghi:</b> ${esc(amenities.slice(0,20).join(" · "))}${amenities.length>20 ? ` · +${fmt(amenities.length-20)} mục khác` : ""}</div>${room.images?.length>1 ? `<div class="room-images-mini">${room.images.slice(1,6).map((img) => `<img src="${esc(safeUrl(img.url))}" loading="lazy" alt="Ảnh phòng">`).join("")}</div>` : ""}</div></article>`;
  }).join("")}</div>${items.length ? "" : '<div class="empty-view"><b>Chưa có dữ liệu phòng</b></div>'}`;
}

function renderPrices(items) {
  els.detailContent.innerHTML = `${heading("Giá theo ngày và thị trường", "Giá VND/USD tách biệt với ngôn ngữ hiển thị", items.length)}<div class="table-wrap"><table class="simple-table"><thead><tr><th>Loại phòng</th><th>Loại giá</th><th>Check-in</th><th>Check-out</th><th>Giá</th><th>Thuế</th><th>Ngôn ngữ</th><th>Ngày ghi nhận</th></tr></thead><tbody>${items.map((item) => `<tr><td>${esc(text(item.room_name,item.room_type_id ? `Room #${item.room_type_id}`:"Toàn khách sạn"))}</td><td>${esc(item.price_type)}</td><td>${esc(item.check_in)}</td><td>${esc(item.check_out)}</td><td class="money">${item.price === null ? "NULL" : `${esc(new Intl.NumberFormat("vi-VN",{maximumFractionDigits:2}).format(Number(item.price)))} ${esc(item.currency)}`}</td><td>${item.tax_included === null ? "—" : item.tax_included ? "Đã gồm" : "Chưa gồm"}</td><td>${esc(item.language)}</td><td>${esc(item.captured_date)}</td></tr>`).join("")}</tbody></table></div>${items.length ? "" : '<div class="empty-view"><b>Chưa có dữ liệu giá</b></div>'}`;
}

function renderPolicies(items) {
  els.detailContent.innerHTML = `${heading("Chính sách khách sạn", "Nhận/trả phòng, trẻ em, thú cưng, thanh toán và các quy định", items.length)}<div class="policy-list">${items.map((item) => `<article class="policy-item"><span class="policy-code">${esc(text(item.policy_code,"other"))}</span><h3>${esc(text(item.title,item.policy_code))}</h3><p>${esc(text(item.description))}</p></article>`).join("")}</div>${items.length ? "" : '<div class="empty-view"><b>Chưa có chính sách</b></div>'}`;
}

function renderNearby(items) {
  const groups = new Map(); for (const item of items) { const key=text(item.category_name || item.category_code,"Khác"); if (!groups.has(key)) groups.set(key,[]); groups.get(key).push(item); }
  els.detailContent.innerHTML = `${heading("Vị trí và địa điểm lân cận", "Giao thông, mua sắm, điểm nổi bật và khoảng cách", items.length)}${[...groups].map(([category,values]) => `<div class="group"><h3 class="group-title">${esc(category)}</h3><div class="nearby-list">${values.map((item) => `<article class="nearby-item"><h3>${esc(text(item.name))}</h3><p>${esc(text(item.description,""))}</p><div class="nearby-meta"><span>${esc(text(item.distance_text,item.distance_km !== null ? `${item.distance_km} km`:"Chưa rõ khoảng cách"))}</span>${item.arrival_type ? `<span>${esc(item.arrival_type)}</span>` : ""}</div></article>`).join("")}</div></div>`).join("") || '<div class="empty-view"><b>Chưa có địa điểm lân cận</b></div>'}`;
}

function renderRaw(payload) {
  els.detailContent.innerHTML = `${heading("Dữ liệu thô", "Raw JSON được giữ lại để kiểm tra và tái phân tích")}<pre class="raw-view">${esc(JSON.stringify({hotel:payload.hotel, translations:payload.translations},null,2))}</pre>`;
}

let searchTimer;
els.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer=setTimeout(() => {state.search=els.search.value.trim(); state.offset=0; loadHotels();},350); });
for (const [element,key] of [[els.locale,"locale"],[els.currency,"currency"],[els.star,"star"],[els.status,"status"],[els.sort,"sort"]]) element.addEventListener("change", () => {state[key]=element.value; state.offset=0; loadHotels();});
els.prev.addEventListener("click", () => {state.offset=Math.max(0,state.offset-state.limit); loadHotels(); window.scrollTo({top:0,behavior:"smooth"});});
els.next.addEventListener("click", () => {state.offset+=state.limit; loadHotels(); window.scrollTo({top:0,behavior:"smooth"});});
$("#refresh-hotels").addEventListener("click", () => {loadHealth(); loadHotels(); showToast("Đang làm mới dữ liệu từ PostgreSQL.");});
$("#back-to-list").addEventListener("click", () => closeHotel());
els.detailLocale.addEventListener("change", async () => {state.locale=els.detailLocale.value; els.locale.value=state.locale; state.sections={}; await openHotel(state.hotelId,false);});
els.tabs.addEventListener("click", (event) => {const button=event.target.closest("button[data-section]"); if (button) activateSection(button.dataset.section);});
window.addEventListener("popstate", () => {const id=new URLSearchParams(location.search).get("hotel"); if (id) openHotel(id,false); else closeHotel(false);});

async function init() {
  await Promise.all([loadHealth(), loadHotels()]);
  const id = new URLSearchParams(location.search).get("hotel"); if (id) await openHotel(id,false);
}
init().catch((error) => {els.error.textContent=error.message; els.error.classList.remove("hidden");});
