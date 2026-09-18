const state = {
  schema: [], tableMap: new Map(), currentTable: null, rows: [], columns: [],
  total: 0, limit: 50, offset: 0, search: "", locale: "", nullColumn: "",
  nullMode: "null", sort: "", direction: "asc", selectedKey: null, detail: null,
};

const $ = (selector) => document.querySelector(selector);
const els = {
  tableList: $("#table-list"), tableTitle: $("#table-title"), tableDescription: $("#table-description"),
  recordTotal: $("#record-total"), stats: $("#stats-strip"), head: $("#data-head"), body: $("#data-body"),
  loading: $("#loading"), empty: $("#empty-state"), error: $("#error-banner"),
  search: $("#search-input"), locale: $("#locale-filter"), nullColumn: $("#null-column"),
  nullMode: $("#null-mode"), pageSize: $("#page-size"), prev: $("#prev-page"), next: $("#next-page"),
  pageNumber: $("#page-number"), pageSummary: $("#page-summary"), inspector: $("#inspector"),
  placeholder: $("#inspector-placeholder"), tabData: $("#tab-data"), tabRelations: $("#tab-relations"),
  tabJson: $("#tab-json"), jsonView: $("#json-view"), relationCount: $("#relation-count"),
  dbStatus: $("#db-status"), toast: $("#toast"),
};

function formatNumber(value) { return new Intl.NumberFormat("vi-VN").format(value ?? 0); }
function compactNumber(value) {
  const n = Number(value || 0);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 100_000 ? 0 : 1)}K`;
  return String(n);
}
function isObject(value) { return value && typeof value === "object"; }
function displayValue(value, full = false) {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value;
  if (isObject(value)) return JSON.stringify(value, null, full ? 2 : 0);
  return String(value);
}
function toast(message) {
  els.toast.textContent = message; els.toast.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => els.toast.classList.remove("show"), 2600);
}
function showError(message = "") {
  els.error.textContent = message; els.error.classList.toggle("hidden", !message);
}
async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function tableIcon(name) {
  if (name.includes("image")) return "▧";
  if (name.includes("location") || name.includes("nearby")) return "⌖";
  if (name.includes("price")) return "$";
  if (name.includes("error")) return "!";
  if (name.includes("run")) return "▶";
  return "▤";
}

async function loadHealth() {
  try {
    const result = await api("/api/health");
    els.dbStatus.className = "db-status online";
    els.dbStatus.innerHTML = `<span></span>${result.database} • Đã kết nối`;
  } catch (error) {
    els.dbStatus.className = "db-status error";
    els.dbStatus.innerHTML = `<span></span>Mất kết nối`;
  }
}

async function loadSchema(refresh = false) {
  const payload = await api(`/api/schema${refresh ? "?refresh=1" : ""}`);
  state.schema = payload.tables; state.tableMap = new Map(payload.tables.map((table) => [table.name, table]));
  renderSidebar();
  const preferred = new URLSearchParams(location.search).get("table");
  const initial = state.tableMap.has(preferred) ? preferred : state.tableMap.has("hotels") ? "hotels" : payload.tables[0]?.name;
  if (!state.currentTable && initial) await selectTable(initial);
}

function renderSidebar() {
  els.tableList.replaceChildren();
  for (const table of state.schema) {
    const button = document.createElement("button"); button.className = "table-button";
    if (table.name === state.currentTable) button.classList.add("active");
    const icon = document.createElement("span"); icon.className = "table-icon"; icon.textContent = tableIcon(table.name);
    const name = document.createElement("span"); name.className = "table-name"; name.textContent = table.name;
    const count = document.createElement("span"); count.className = "table-count"; count.textContent = compactNumber(table.row_estimate);
    button.append(icon, name, count); button.addEventListener("click", () => selectTable(table.name));
    els.tableList.append(button);
  }
}

async function selectTable(name) {
  if (!state.tableMap.has(name)) return;
  state.currentTable = name; state.offset = 0; state.search = ""; state.locale = "";
  state.nullColumn = ""; state.sort = ""; state.direction = "asc"; state.selectedKey = null;
  els.search.value = ""; closeRecord(); renderSidebar(); configureFilters();
  const url = new URL(location.href); url.searchParams.set("table", name); history.replaceState({}, "", url);
  await Promise.all([loadRows(), loadStats()]);
}

function configureFilters() {
  const table = state.tableMap.get(state.currentTable); const columnNames = table.columns.map((c) => c.name);
  const localeColumn = columnNames.includes("locale") ? "locale" : columnNames.includes("language") ? "language" : null;
  els.locale.classList.toggle("hidden", !localeColumn); els.locale.replaceChildren();
  if (localeColumn) {
    for (const [value, label] of [["", "Tất cả ngôn ngữ"], ["vi", "locale: vi"], ["en", "locale: en"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = label; els.locale.append(option);
    }
  }
  els.nullColumn.replaceChildren(new Option("Tất cả dữ liệu", ""));
  for (const column of table.columns) els.nullColumn.append(new Option(`Cột: ${column.name}`, column.name));
  els.nullMode.disabled = true;
}

function queryString() {
  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.search) params.set("search", state.search);
  if (state.locale) params.set("locale", state.locale);
  if (state.nullColumn) { params.set("null_column", state.nullColumn); params.set("null_mode", state.nullMode); }
  if (state.sort) { params.set("sort", state.sort); params.set("direction", state.direction); }
  return params.toString();
}

async function loadRows() {
  if (!state.currentTable) return;
  els.loading.classList.remove("hidden"); els.empty.classList.add("hidden"); showError();
  try {
    const payload = await api(`/api/table/${encodeURIComponent(state.currentTable)}?${queryString()}`);
    state.rows = payload.rows; state.columns = payload.columns; state.total = payload.total;
    state.sort = payload.sort; state.direction = payload.direction;
    renderTable(); renderPagination();
  } catch (error) {
    state.rows = []; renderTable(); showError(error.message);
  } finally { els.loading.classList.add("hidden"); }
}

async function loadStats() {
  els.stats.className = "stats-strip skeleton-line"; els.stats.replaceChildren();
  try {
    const payload = await api(`/api/table/${encodeURIComponent(state.currentTable)}/stats`);
    renderStats(payload);
  } catch (error) {
    els.stats.className = "stats-strip";
    const item = document.createElement("div"); item.className = "stat-item"; item.textContent = `Không đọc được thống kê: ${error.message}`; els.stats.append(item);
  }
}

function renderStats(payload) {
  els.stats.className = "stats-strip"; els.stats.replaceChildren();
  const interesting = payload.fields.filter((field) => !["id", "created_at", "updated_at"].includes(field.column));
  const selected = interesting.slice(0, 6);
  for (const field of selected) {
    const item = document.createElement("div"); item.className = "stat-item";
    const name = document.createElement("div"); name.className = "stat-name"; name.textContent = field.column;
    const value = document.createElement("div"); value.className = "stat-value";
    value.append(document.createTextNode(formatNumber(field.filled)));
    const percent = document.createElement("span"); percent.textContent = `${field.percent}% có dữ liệu`; value.append(percent);
    const bar = document.createElement("div"); bar.className = "mini-bar"; const fill = document.createElement("i"); fill.style.width = `${field.percent}%`; bar.append(fill);
    item.append(name, value, bar); els.stats.append(item);
  }
}

function renderTable() {
  const table = state.tableMap.get(state.currentTable); els.tableTitle.textContent = state.currentTable;
  els.tableDescription.textContent = `${state.columns.length || table.columns.length} cột • Khóa chính: ${table.primary_key.join(", ") || "không có"}`;
  els.recordTotal.textContent = `${formatNumber(state.total)} bản ghi`; els.head.replaceChildren(); els.body.replaceChildren();
  const headerRow = document.createElement("tr");
  for (const column of state.columns) {
    const th = document.createElement("th"); th.className = "sortable"; th.textContent = column.name;
    if (table.primary_key.includes(column.name)) th.classList.add("key-column");
    if (state.sort === column.name) th.textContent += state.direction === "asc" ? " ↑" : " ↓";
    th.addEventListener("click", () => {
      state.direction = state.sort === column.name && state.direction === "asc" ? "desc" : "asc";
      state.sort = column.name; state.offset = 0; loadRows();
    });
    headerRow.append(th);
  }
  els.head.append(headerRow);
  for (const entry of state.rows) {
    const tr = document.createElement("tr");
    if (state.selectedKey && JSON.stringify(entry.key) === JSON.stringify(state.selectedKey)) tr.classList.add("selected");
    tr.addEventListener("click", () => loadRecord(entry.key, tr));
    for (const column of state.columns) tr.append(valueCell(entry.values[column.name]));
    els.body.append(tr);
  }
  els.empty.classList.toggle("hidden", state.rows.length !== 0);
}

function valueCell(value) {
  const td = document.createElement("td");
  if (value === null || value === undefined) {
    const badge = document.createElement("span"); badge.className = "null-badge"; badge.textContent = "NULL"; td.append(badge); return td;
  }
  if (typeof value === "boolean") {
    const badge = document.createElement("span"); badge.className = `bool-badge ${value}`; badge.textContent = value ? "TRUE" : "FALSE"; td.append(badge); return td;
  }
  const text = displayValue(value); td.textContent = text; td.title = text;
  if (isObject(value)) td.classList.add("json-cell");
  return td;
}

function renderPagination() {
  const start = state.total ? state.offset + 1 : 0; const end = Math.min(state.offset + state.limit, state.total);
  els.pageSummary.textContent = `${formatNumber(start)}–${formatNumber(end)} / ${formatNumber(state.total)}`;
  els.pageNumber.textContent = Math.floor(state.offset / state.limit) + 1;
  els.prev.disabled = state.offset === 0; els.next.disabled = state.offset + state.limit >= state.total;
}

async function loadRecord(key, rowElement) {
  if (!Object.keys(key || {}).length) { toast("Bảng này không có khóa chính để mở chi tiết."); return; }
  state.selectedKey = key; document.querySelectorAll(".data-table tbody tr").forEach((row) => row.classList.remove("selected")); rowElement.classList.add("selected");
  els.inspector.classList.add("open"); els.placeholder.classList.remove("hidden"); els.placeholder.querySelector("strong").textContent = "Đang tải...";
  els.placeholder.querySelector("span").textContent = "Đọc bản ghi và các quan hệ khóa ngoại.";
  try {
    const payload = await api(`/api/record/${encodeURIComponent(state.currentTable)}?key=${encodeURIComponent(JSON.stringify(key))}`);
    state.detail = payload; renderRecord();
  } catch (error) { toast(error.message); }
}

function renderRecord() {
  const { values, relations } = state.detail; els.placeholder.classList.add("hidden");
  els.tabData.replaceChildren(); els.tabRelations.replaceChildren();
  for (const [name, value] of Object.entries(values)) {
    const row = document.createElement("div"); row.className = "field-row";
    const label = document.createElement("div"); label.className = "field-name"; label.textContent = name;
    const content = document.createElement("div"); content.className = "field-value";
    if (value === null || value === undefined) { const badge = document.createElement("span"); badge.className = "null-badge"; badge.textContent = "NULL"; content.append(badge); }
    else content.textContent = displayValue(value, true);
    row.append(label, content); els.tabData.append(row);
  }
  els.relationCount.textContent = relations.length ? `(${relations.length})` : "";
  if (!relations.length) {
    const empty = document.createElement("div"); empty.className = "inspector-placeholder"; empty.textContent = "Không có quan hệ khóa ngoại."; els.tabRelations.append(empty);
  }
  for (const relation of relations) {
    const item = document.createElement("div"); item.className = "relation-item";
    const icon = document.createElement("div"); icon.className = "relation-icon"; icon.textContent = relation.direction === "outbound" ? "→" : "←";
    const info = document.createElement("div"); info.className = "relation-info";
    const title = document.createElement("strong"); title.textContent = relation.table;
    const constraint = document.createElement("span"); constraint.textContent = relation.constraint; info.append(title, constraint);
    const count = document.createElement("div"); count.className = "relation-count"; count.textContent = `${formatNumber(relation.count)} bản ghi`;
    item.append(icon, info, count); item.addEventListener("click", () => selectTable(relation.table)); els.tabRelations.append(item);
  }
  els.jsonView.textContent = JSON.stringify(values, null, 2); activateTab("data");
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
  els.tabData.classList.toggle("hidden", name !== "data"); els.tabRelations.classList.toggle("hidden", name !== "relations"); els.tabJson.classList.toggle("hidden", name !== "json");
}
function closeRecord() {
  state.detail = null; state.selectedKey = null; els.inspector.classList.remove("open"); els.placeholder.classList.remove("hidden");
  const strong = els.placeholder.querySelector("strong"); const span = els.placeholder.querySelector("span");
  strong.textContent = "Chọn một bản ghi"; span.textContent = "Nhấp vào một hàng để xem đầy đủ giá trị và quan hệ.";
  els.tabData.classList.add("hidden"); els.tabRelations.classList.add("hidden"); els.tabJson.classList.add("hidden");
}

function exportJson() {
  const payload = state.rows.map((entry) => entry.values); const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `${state.currentTable}_page_${Math.floor(state.offset / state.limit) + 1}.json`; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000); toast(`Đã xuất ${payload.length} bản ghi trên trang hiện tại.`);
}

let searchTimer;
els.search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = els.search.value.trim(); state.offset = 0; loadRows(); }, 350); });
els.locale.addEventListener("change", () => { state.locale = els.locale.value; state.offset = 0; loadRows(); });
els.nullColumn.addEventListener("change", () => { state.nullColumn = els.nullColumn.value; els.nullMode.disabled = !state.nullColumn; state.offset = 0; loadRows(); });
els.nullMode.addEventListener("change", () => { state.nullMode = els.nullMode.value; state.offset = 0; loadRows(); });
els.pageSize.addEventListener("change", () => { state.limit = Number(els.pageSize.value); state.offset = 0; loadRows(); });
els.prev.addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadRows(); });
els.next.addEventListener("click", () => { state.offset += state.limit; loadRows(); });
$("#clear-filters").addEventListener("click", () => { state.search = state.locale = state.nullColumn = ""; state.offset = 0; els.search.value = ""; configureFilters(); loadRows(); });
$("#export-json").addEventListener("click", exportJson);
$("#refresh-schema").addEventListener("click", async () => { await loadSchema(true); await loadRows(); toast("Đã làm mới schema PostgreSQL."); });
$("#close-inspector").addEventListener("click", closeRecord);
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => activateTab(tab.dataset.tab)));

Promise.all([loadHealth(), loadSchema()]).catch((error) => { showError(error.message); els.loading.classList.add("hidden"); });
