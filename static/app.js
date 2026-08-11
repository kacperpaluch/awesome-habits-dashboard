"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const fmt = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 1 });
const state = { start: null, end: null, range: "year", habit: "", list: "", period: "", bounds: null };
const historyState = { tab: "imports", page: 1, perPage: 10, dateFrom: "", dateTo: "" };
const months = ["Sty", "Lut", "Mar", "Kwi", "Maj", "Cze", "Lip", "Sie", "Wrz", "Paź", "Lis", "Gru"];
const weekdays = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}
function iso(day) { return day.toISOString().slice(0, 10); }
function parseDate(value) { return new Date(`${value}T12:00:00`); }
function toast(message, error = false) {
  const box = $("#toast"); box.textContent = message; box.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => box.className = "toast", 3800);
}
async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Błąd HTTP ${response.status}`);
  return body;
}
function query() {
  const params = new URLSearchParams();
  for (const key of ["start", "end", "habit", "list", "period"]) if (state[key]) params.set(key, state[key]);
  return params.toString();
}
function setRange(kind) {
  state.range = kind; $$("[data-range]").forEach((button) => button.classList.toggle("active", button.dataset.range === kind));
  $("#customRange").classList.toggle("hidden", kind !== "custom");
  if (kind === "custom" || !state.bounds?.max_date) return;
  const end = parseDate(state.bounds.max_date); let start = new Date(end);
  if (kind === "month") start.setMonth(start.getMonth() - 1);
  if (kind === "quarter") start.setMonth(start.getMonth() - 3);
  if (kind === "half") start.setMonth(start.getMonth() - 6);
  if (kind === "year") start.setFullYear(start.getFullYear() - 1);
  state.start = kind === "all" ? state.bounds.min_date : iso(start); state.end = state.bounds.max_date;
  loadDashboard();
}
function populateSelect(selector, values, label) {
  const select = $(selector); const current = select.value;
  select.innerHTML = `<option value="">${label}</option>` + values.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  select.value = values.includes(current) ? current : "";
}

async function loadConfig() {
  const config = await api("/api/config");
  $("#webhookUrl").textContent = config.webhook_url;
  $("#uploadLimit").textContent = config.max_upload_mb;
  renderLatest(config.latest_event, config.latest_import);
  return config;
}

function renderBackupStatus(status) {
  const latest = status.latest;
  $("#backupStatus").innerHTML = latest ? `<strong>${status.healthy ? "Backup sprawdzony" : "Backup wymaga uwagi"}</strong><small>${escapeHtml(latest.file)} · ${latest.size_kb} KB · codziennie ${escapeHtml(status.backup_time)} · retencja ${status.keep} kopii</small>` : `<strong>Brak backupu</strong><small>Pierwszy powstanie po ${escapeHtml(status.backup_time)}, gdy baza będzie zawierała dane.</small>`;
}
function historyQuery() {
  const params = new URLSearchParams({ page: historyState.page, per_page: historyState.perPage });
  if (historyState.dateFrom) params.set("date_from", historyState.dateFrom);
  if (historyState.dateTo) params.set("date_to", historyState.dateTo);
  return params.toString();
}
function renderHistoryPagination(pagination) {
  $("#historyCount").textContent = `${pagination.total} wpisów`;
  $("#historyPage").textContent = `Strona ${pagination.page} z ${pagination.pages}`;
  $("#historyPrevious").disabled = !pagination.has_previous; $("#historyNext").disabled = !pagination.has_next;
}
async function loadHistory() {
  const backups = historyState.tab === "backups";
  const result = await api(`/${backups ? "api/backups" : "api/imports"}?${historyQuery()}`);
  const items = backups ? result.backups : result.items;
  if (backups) renderBackupStatus(result);
  $("#historyList").innerHTML = items.length ? items.map((item) => backups ? `<div class="backup-row"><div><strong>${item.kind === "pre_restore" ? "Kopia przed przywróceniem" : item.kind === "manual" ? "Backup ręczny" : "Backup automatyczny"}</strong><small>${new Date(item.modified).toLocaleString("pl-PL")} · ${item.size_kb} KB</small></div><div><a href="/api/backups/${encodeURIComponent(item.file)}/download">Pobierz</a><button data-restore="${escapeHtml(item.file)}">Przywróć</button></div></div>` : `<div class="import-row"><div><strong>${item.status === "failed" ? (item.source === "webhook" ? "Webhook odrzucony" : "Import odrzucony") : item.source === "webhook" ? "Webhook odebrany" : "Import z interfejsu"}</strong><small>${new Date(item.imported_at).toLocaleString("pl-PL")} · ${escapeHtml(item.filename)}</small></div><div><strong>${item.status === "failed" ? escapeHtml(item.error) : `${item.rows_count} rekordów · ${item.changed ? "dane zmienione" : "bez zmian"}`}</strong><small>${item.status === "failed" ? "Poprzednie dane pozostały bez zmian" : `${escapeHtml(item.min_date)}–${escapeHtml(item.max_date)}`}</small></div></div>`).join("") : "<p class='latest-import'>Brak wpisów w wybranym zakresie.</p>";
  renderHistoryPagination(result.pagination);
  $$('[data-restore]').forEach((button) => button.addEventListener("click", () => restoreServerBackup(button.dataset.restore)));
}
async function loadBackupSummary() {
  renderBackupStatus(await api("/api/backups?page=1&per_page=1"));
}

async function restoreServerBackup(filename) {
  const confirmation = prompt(`Przywrócenie zastąpi bieżące dane. Wpisz PRZYWRÓĆ, aby użyć kopii:\n${filename}`);
  if (confirmation !== "PRZYWRÓĆ") return;
  try {
    const result = await api(`/api/backups/${encodeURIComponent(filename)}/restore`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirmation }) });
    toast(`Dane przywrócone. Kopia bezpieczeństwa: ${result.safety_backup}`); await refreshAfterRestore();
  } catch (error) { toast(error.message, true); }
}

async function restoreUploadedBackup(file) {
  if (!file) return;
  const confirmation = prompt("Przywrócenie zastąpi bieżące dane. Wpisz PRZYWRÓĆ, aby kontynuować.");
  if (confirmation !== "PRZYWRÓĆ") { $("#backupFileInput").value = ""; return; }
  const form = new FormData(); form.append("file", file);
  try {
    const result = await api(`/api/backups/restore-upload?confirmation=${encodeURIComponent(confirmation)}`, { method: "POST", body: form });
    toast(`Dane przywrócone z pliku. Kopia bezpieczeństwa: ${result.safety_backup}`); await refreshAfterRestore();
  } catch (error) { toast(error.message, true); }
  finally { $("#backupFileInput").value = ""; }
}

async function refreshAfterRestore() {
  state.bounds = null; state.start = null; state.end = null;
  await Promise.all([loadConfig(), loadBackupSummary(), loadHistory(), loadDashboard(true)]);
}
function renderLatest(event, latestSuccessful) {
  $("#latestImport").innerHTML = event ? event.status === "failed"
    ? `<strong>${event.source === "webhook" ? "Ostatni webhook odrzucony" : "Ostatni import odrzucony"}</strong><br>${new Date(event.imported_at).toLocaleString("pl-PL")} · ${escapeHtml(event.error)}`
    : `<strong>${event.source === "webhook" ? "Ostatni webhook" : "Ostatni import"}</strong><br>${escapeHtml(event.filename)} · ${event.rows_count} rekordów<br>${new Date(event.imported_at).toLocaleString("pl-PL")}`
    : "Nie zaimportowano jeszcze żadnego pliku.";
  const dataState = $("#dataState"); dataState.classList.toggle("ready", Boolean(latestSuccessful));
  dataState.lastChild.textContent = latestSuccessful ? ` ${latestSuccessful.rows_count} rekordów` : " Brak danych";
}

async function importFile(file) {
  if (!file) return;
  const form = new FormData(); form.append("file", file);
  $("#importButton").disabled = true; $("#importButton").textContent = "Importuję…";
  try {
    const result = await api("/api/import", { method: "POST", body: form });
    toast(`Zaimportowano ${result.rows} rekordów z ${result.habits} nawyków.`);
    await Promise.all([loadConfig(), loadBackupSummary(), loadHistory()]); state.bounds = null; await loadDashboard(true);
  } catch (error) { toast(error.message, true); }
  finally { $("#importButton").disabled = false; $("#importButton").textContent = "Importuj CSV"; $("#fileInput").value = ""; }
}

async function loadDashboard(initial = false) {
  try {
    const data = await api(`/api/dashboard?${query()}`);
    if (!data.bounds.min_date) {
      $("#emptyWelcome").classList.remove("hidden"); $("#dashboard").classList.add("hidden"); return;
    }
    $("#emptyWelcome").classList.add("hidden"); $("#dashboard").classList.remove("hidden");
    if (!state.bounds || initial) {
      state.bounds = data.bounds;
      if (!state.start || initial) {
        const end = parseDate(data.bounds.max_date); const start = new Date(end); start.setFullYear(start.getFullYear() - 1);
        state.start = iso(start) < data.bounds.min_date ? data.bounds.min_date : iso(start); state.end = data.bounds.max_date;
        if (initial) return loadDashboard(false);
      }
    }
    render(data);
  } catch (error) { toast(error.message, true); }
}

function render(data) {
  const summary = data.summary;
  $("#metricRate").textContent = summary.rate == null ? "—" : `${fmt.format(summary.rate)}%`; $("#metricDone").textContent = fmt.format(summary.done);
  $("#metricMissed").textContent = fmt.format(summary.missed); $("#metricProgress").textContent = fmt.format(summary.in_progress); $("#metricPerfect").textContent = fmt.format(summary.perfect_days);
  $("#metricRateSub").textContent = `${summary.resolved} zakończonych okresów`;
  $("#dataRange").textContent = `${state.start || data.bounds.min_date} — ${state.end || data.bounds.max_date} · ${summary.records} rekordów`;
  populateSelect("#habitFilter", data.options.habits, "Wszystkie nawyki"); populateSelect("#listFilter", data.options.lists, "Wszystkie listy"); populateSelect("#periodFilter", data.options.periods, "Dzienny i tygodniowy");
  $("#habitFilter").value = state.habit; $("#listFilter").value = state.list; $("#periodFilter").value = state.period;
  renderToday(data.analytics.today); renderHeatmap(data.heatmap); renderTrend(data.analytics.trends.daily);
  renderHabits(data.habits); renderWeekdays(data.analytics.weekdays); renderMonthly(data.analytics.monthly); renderRegularity(data.analytics.regularity);
}

function renderToday(today) {
  $("#todayMeta").textContent = today.total ? `${today.done}/${today.total} zakończone` : "Brak bieżącego okresu w eksporcie";
  $("#todayVerdict").textContent = !today.total ? "Czekamy na dane" : !today.pending.length ? "Wszystko pod kontrolą" : `${today.pending.length} ${today.pending.length === 1 ? "cel czeka" : "cele czekają"}`;
  $("#todayList").innerHTML = today.pending.length ? today.pending.map((item) => `<div class="today-row"><div><strong>${escapeHtml(item.name)}</strong><small>${item.period === "Weekly" ? "Cel tygodniowy" : "Cel dzienny"} · ${progressText(item)}</small></div><span class="stake">W trakcie${item.streak ? ` · streak ${item.streak} ${item.unit === "week" ? "tyg." : "dni"}` : ""}</span></div>`).join("") : `<p class="today-clear">${today.total ? "Świetna robota — wszystkie widoczne cele są wykonane." : "Nowy eksport uzupełni ten panel."}</p>`;
}

function progressText(item) {
  const unit = escapeHtml(item.value_unit || "");
  if (item.type === "Breaking" && item.goal === 0) return item.quantity === 0 ? "cel zachowany do tej pory" : `${fmt.format(item.quantity)} ${unit} · cel przekroczony`;
  if (item.type === "Breaking") return `${fmt.format(item.quantity)} / ${fmt.format(item.goal)} ${unit} · pozostało ${fmt.format(Math.max(0, item.goal - item.quantity))} ${unit}`;
  const percent = item.goal > 0 ? Math.min(100, item.quantity / item.goal * 100) : 0;
  return `${fmt.format(item.quantity)} / ${fmt.format(item.goal)} ${unit} · ${fmt.format(percent)}%`;
}

function renderHabits(habits) {
  $("#emptyState").classList.toggle("hidden", habits.length > 0);
  $("#habitRows").innerHTML = habits.map((habit) => { const hasRate = habit.rate != null; return `<tr><td><div class="habit-name"><span class="habit-dot">${escapeHtml(habit.name[0])}</span><span><strong>${escapeHtml(habit.name)}</strong><small>${escapeHtml(habit.list || habit.type)} · ${escapeHtml(habit.period)}</small></span></div></td><td class="rate-cell"><div class="rate-top"><strong>${hasRate ? `${fmt.format(habit.rate)}%` : "—"}</strong><span>${hasRate ? (habit.rate >= 80 ? "dobry rytm" : "do poprawy") : "brak zamkniętych"}</span></div><div class="progress"><i style="width:${hasRate ? habit.rate : 0}%"></i></div></td><td>${habit.done}</td><td>${habit.missed}</td><td class="pending-count">${habit.in_progress || "—"}</td><td>${habit.current_streak} ${habit.streak_unit === "week" ? "tyg." : "dni"}</td><td>${fmt.format(habit.average)} ${escapeHtml(habit.unit)}</td><td><button class="row-open" data-habit="${escapeHtml(habit.name)}" data-period="${escapeHtml(habit.period)}" aria-label="Szczegóły">›</button></td></tr>`; }).join("");
  $$(".row-open").forEach((button) => button.addEventListener("click", () => showDetail(button.dataset.habit, button.dataset.period)));
}

function startOfWeek(day) { const result = new Date(day); result.setDate(result.getDate() - ((result.getDay() + 6) % 7)); return result; }
function renderHeatmap(values) {
  const container = $("#heatmap"), labels = $("#monthLabels"); container.innerHTML = ""; labels.innerHTML = "";
  if (!state.start || !state.end) return;
  const lookup = new Map(values.map((item) => [item.date, item])); const actualStart = parseDate(state.start), actualEnd = parseDate(state.end);
  const gridStart = startOfWeek(actualStart), gridEnd = new Date(startOfWeek(actualEnd)); gridEnd.setDate(gridEnd.getDate() + 6);
  const totalDays = Math.round((gridEnd - gridStart) / 86400000) + 1, weeks = Math.ceil(totalDays / 7); container.style.gridTemplateColumns = `repeat(${weeks},13px)`;
  for (let i = 0; i < totalDays; i++) {
    const day = new Date(gridStart); day.setDate(day.getDate() + i); const dayIso = iso(day), item = lookup.get(dayIso), rate = item?.rate || 0;
    const cell = document.createElement("button"); cell.className = `heat-cell${day < actualStart || day > actualEnd ? " outside" : ""}${item?.in_progress ? " in-progress" : ""}`; cell.dataset.level = rate === 0 ? 0 : rate < 40 ? 1 : rate < 70 ? 2 : rate < 100 ? 3 : 4;
    cell.title = item ? `${dayIso}: ${item.done}/${item.total} wykonane${item.in_progress ? ` · ${item.in_progress} w trakcie` : ""} (${item.rate}%)` : `${dayIso}: brak danych`; container.append(cell);
  }
  let lastMonth = -1;
  for (let week = 0; week < weeks; week++) { const day = new Date(gridStart); day.setDate(day.getDate() + week * 7); const label = document.createElement("span"); label.style.width = "17px"; if (day.getMonth() !== lastMonth) { label.textContent = months[day.getMonth()]; lastMonth = day.getMonth(); } labels.append(label); }
  labels.style.width = `${weeks * 17 + 30}px`; $("#heatmapCaption").textContent = `${values.length} dni z danymi · udział wykonanych nawyków dziennych`;
}

function renderTrend(points) {
  const canvas = $("#trendChart"), ratio = window.devicePixelRatio || 1, width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = width * ratio; canvas.height = height * ratio; const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio); ctx.clearRect(0, 0, width, height);
  const pad = { left: 34, right: 12, top: 12, bottom: 25 }, w = width - pad.left - pad.right, h = height - pad.top - pad.bottom;
  ctx.font = "10px sans-serif"; ctx.fillStyle = "#73776f"; ctx.strokeStyle = "#dcddd7"; ctx.lineWidth = 1;
  [0, 25, 50, 75, 100].forEach((value) => { const y = pad.top + h - value / 100 * h; ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); ctx.fillText(`${value}%`, 0, y + 3); });
  if (points.length < 2) return;
  const draw = (key, color, lineWidth) => { ctx.beginPath(); points.forEach((point, i) => { const x = pad.left + i / (points.length - 1) * w, y = pad.top + h - point[key] / 100 * h; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.strokeStyle = color; ctx.lineWidth = lineWidth; ctx.stroke(); };
  draw("rate", "rgba(115,119,111,.35)", 1); draw("avg30", "#d78b58", 2); draw("avg7", "#355f4b", 2.5);
}
function renderWeekdays(items) { $("#weekdayBars").innerHTML = items.map((item) => `<div class="bar-row"><span>${weekdays[item.day]}</span><div class="bar-track"><i style="width:${item.rate || 0}%"></i></div><strong>${item.rate == null ? "—" : `${fmt.format(item.rate)}%`}</strong></div>`).join(""); }
function renderMonthly(items) { $("#monthlyGrid").innerHTML = items.map((item) => `<div class="month-card"><span>${escapeHtml(item.month)}</span><strong>${item.rate == null ? "—" : `${fmt.format(item.rate)}%`}</strong><small>${item.perfect_days} idealnych dni · ${item.records} wpisów</small></div>`).join("") || `<p class="empty">Brak danych miesięcznych.</p>`; }
function renderRegularity(item) { const value = item.weekly_stddev; $("#regularity").innerHTML = `<strong>${value == null ? "—" : fmt.format(value)}</strong><p>${value == null ? "Potrzeba co najmniej dwóch tygodni danych." : `Odchylenie wyników z ${item.weeks} tygodni. Im niżej, tym równiejszy rytm.`}</p>`; }

async function showDetail(name, period) {
  try {
    const params = new URLSearchParams(query()); params.set("period", period);
    const item = await api(`/api/habits/${encodeURIComponent(name)}?${params}`);
    $("#detailContent").innerHTML = `<p class="eyebrow">${escapeHtml(item.period)} · ${escapeHtml(item.type)}</p><h2>${escapeHtml(item.name)}</h2><div class="detail-kpis"><div class="detail-kpi"><span>Skuteczność</span><strong>${item.rate == null ? "—" : `${fmt.format(item.rate)}%`}</strong></div><div class="detail-kpi"><span>W trakcie</span><strong>${item.in_progress}</strong></div><div class="detail-kpi"><span>Aktualny streak</span><strong>${item.current_streak}</strong></div><div class="detail-kpi"><span>Średnia</span><strong>${fmt.format(item.average)}</strong></div></div><div class="detail-records">${item.records.slice().reverse().map((r) => `<div class="detail-record ${r.state}"><span>${r.date}</span><strong>${fmt.format(r.quantity)} ${escapeHtml(item.unit)} · ${r.state === "complete" ? "wykonane" : r.state === "in_progress" ? "w trakcie" : "niewykonane"}</strong></div>`).join("")}</div>`;
    $("#detailDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function wireEvents() {
  $("#importButton").addEventListener("click", () => $("#fileInput").click()); $$(".import-trigger").forEach((el) => el.addEventListener("click", () => $("#fileInput").click()));
  $("#fileInput").addEventListener("change", (event) => importFile(event.target.files[0]));
  const drop = $(".drop-zone"); ["dragenter", "dragover"].forEach((name) => drop.addEventListener(name, (e) => { e.preventDefault(); drop.classList.add("drag"); })); ["dragleave", "drop"].forEach((name) => drop.addEventListener(name, (e) => { e.preventDefault(); drop.classList.remove("drag"); })); drop.addEventListener("drop", (e) => importFile(e.dataTransfer.files[0]));
  $("#settingsButton").addEventListener("click", () => { $("#settingsDialog").showModal(); Promise.all([loadBackupSummary(), loadHistory()]).catch((error) => toast(error.message, true)); }); $$('[data-close]').forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
  $("#copyWebhook").addEventListener("click", async () => { await navigator.clipboard.writeText($("#webhookUrl").textContent); toast("Adres webhooka skopiowany."); });
  $$("[data-range]").forEach((button) => button.addEventListener("click", () => setRange(button.dataset.range)));
  $("#applyRange").addEventListener("click", () => { state.start = $("#startDate").value; state.end = $("#endDate").value; if (state.start && state.end && state.start <= state.end) loadDashboard(); else toast("Wybierz poprawny zakres dat.", true); });
  [["#habitFilter", "habit"], ["#listFilter", "list"], ["#periodFilter", "period"]].forEach(([selector, key]) => $(selector).addEventListener("change", (e) => { state[key] = e.target.value; loadDashboard(); }));
  $("#clearFilters").addEventListener("click", () => { state.habit = state.list = state.period = ""; loadDashboard(); });
  $("#backupNow").addEventListener("click", async () => { try { const result = await api("/api/backup", { method: "POST" }); toast(`Utworzono backup ${result.backup}`); historyState.tab = "backups"; historyState.page = 1; $$('[data-history-tab]').forEach((button) => button.classList.toggle("active", button.dataset.historyTab === "backups")); await Promise.all([loadBackupSummary(), loadHistory()]); } catch (error) { toast(error.message, true); } });
  $("#restoreUpload").addEventListener("click", () => $("#backupFileInput").click());
  $("#backupFileInput").addEventListener("change", (event) => restoreUploadedBackup(event.target.files[0]));
  $$('[data-history-tab]').forEach((button) => button.addEventListener("click", async () => { historyState.tab = button.dataset.historyTab; historyState.page = 1; $$('[data-history-tab]').forEach((item) => item.classList.toggle("active", item === button)); await loadHistory(); }));
  $("#historyApply").addEventListener("click", async () => { historyState.dateFrom = $("#historyFrom").value; historyState.dateTo = $("#historyTo").value; historyState.page = 1; await loadHistory(); });
  $("#historyClear").addEventListener("click", async () => { $("#historyFrom").value = ""; $("#historyTo").value = ""; historyState.dateFrom = ""; historyState.dateTo = ""; historyState.page = 1; await loadHistory(); });
  $("#historyPrevious").addEventListener("click", async () => { if (historyState.page > 1) { historyState.page -= 1; await loadHistory(); } });
  $("#historyNext").addEventListener("click", async () => { historyState.page += 1; await loadHistory(); });
  window.addEventListener("resize", () => { clearTimeout(window.chartTimer); window.chartTimer = setTimeout(loadDashboard, 150); });
}

wireEvents();
Promise.all([loadConfig(), loadBackupSummary(), loadHistory(), loadDashboard(true)]).catch((error) => toast(error.message, true));
