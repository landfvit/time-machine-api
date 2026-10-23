"use strict";

const $ = (id) => document.getElementById(id);
const INITIAL_STATE = { stock: { frames: 20, wheels: 40 }, orders: { built: 0 }, notes: [] };
const METRICS = [
  { label: "Frames", path: "/stock/frames", group: "stock", key: "frames", element: "stock-frames" },
  { label: "Wheels", path: "/stock/wheels", group: "stock", key: "wheels", element: "stock-wheels" },
  { label: "Bicycles built", path: "/orders/built", group: "orders", key: "built", element: "stock-built" },
];
let timelines = [];
let nextCursor = null;
let timeline = null;
let state = null;
let events = [];
let oldestVersion = 0;
let busy = false;
let apiKeyRequired = false;
let navigation = 0;
let activeTab = "action";
let deliverySelection = "";
let preview = null;
let previewTarget = null;
let resolutions = new Map();
let stalePreview = false;
const retryKeys = new Map();

class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

function validateNumbers(value) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("The browser cannot represent this JSON number. Use the API for this document.");
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) throw new Error("This number exceeds the browser's exact integer range. Use the API for this document.");
  } else if (value !== null && typeof value === "object") {
    for (const entry of Object.values(value)) validateNumbers(entry);
  }
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiKeyRequired && $("api-key").value) headers.set("X-API-Key", $("api-key").value);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  let response;
  try { response = await fetch(path, { ...options, headers }); }
  catch { throw new ApiError("Cannot reach the server. Retry when it is available.", 0, "network_error"); }
  let body;
  try { body = await response.json(); } catch { throw new Error("The server returned an invalid response."); }
  validateNumbers(body);
  if (!response.ok) {
    let message = body.error?.message || `Request failed (${response.status}).`;
    if (typeof body.error?.details?.message === "string") message += " " + body.error.details.message;
    const issues = body.error?.details?.issues;
    if (Array.isArray(issues) && issues.length) message += " " + issues.slice(0, 2).map((issue) => `${issue.location.join(".")}: ${issue.message}`).join(" ");
    throw new ApiError(message, response.status, body.error?.code);
  }
  return body;
}

async function mutate(path, payload) {
  validateNumbers(payload);
  const body = JSON.stringify(payload);
  const signature = path + "\n" + body;
  // retry an uncertain write with the same key and payload.
  if (!retryKeys.has(signature)) retryKeys.set(signature, crypto.randomUUID());
  try {
    const result = await request(path, { method: "POST", body, headers: { "Idempotency-Key": retryKeys.get(signature) } });
    retryKeys.delete(signature);
    return result;
  } catch (error) {
    if (error.status && error.status < 500) retryKeys.delete(signature);
    throw error;
  } finally { if (retryKeys.size > 30) retryKeys.delete(retryKeys.keys().next().value); }
}

function status(message = "", error = false) {
  $("status").textContent = message;
  $("status").parentElement.classList.toggle("error", error);
  $("retry-refresh").hidden = true;
}

function showError(error) {
  status(error.message || "Request failed.", true);
  if (error.status === 409) {
    $("retry-refresh").hidden = false;
    $("retry-refresh").textContent = stalePreview ? "Refresh preview" : "Refresh latest";
  }
}

function metricValue(document, metric) { return document?.[metric.group]?.[metric.key]; }
function numberLabel(value) { return typeof value === "number" ? value.toLocaleString() : "—"; }
function version() { return state?.version ?? 0; }
function head() { return timeline?.head_version ?? 0; }
function isPlainObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function isWorkshop(document) {
  return isPlainObject(document) && isPlainObject(document.stock) && isPlainObject(document.orders) && Array.isArray(document.notes)
    && METRICS.every((metric) => Number.isSafeInteger(metricValue(document, metric)) && metricValue(document, metric) >= 0);
}
function isLatest() { return !!timeline && !!state && version() === head(); }
function readyToAct() { return isLatest() && isWorkshop(state.state); }
function selectedEvent() { return events.find((event) => event.version === version()); }
function selectedDelivery() {
  const event = selectedEvent();
  if (!event || event.operations.length !== 1) return null;
  const operation = event.operations[0];
  if (operation.op !== "increment" || !["/stock/frames", "/stock/wheels"].includes(operation.path) || !Number.isSafeInteger(operation.value) || operation.value <= 0) return null;
  return { event, operation, item: operation.path.split("/")[2] };
}

function positiveQuantity(id) {
  const raw = $(id).value.trim();
  const value = Number(raw);
  if (!raw || !Number.isSafeInteger(value) || value <= 0) throw new Error("Quantity must be a positive whole number within the browser's safe integer range.");
  return value;
}

function setBusy(value) { busy = value; updateControls(); }
function updateControls() {
  const available = !!timeline && !!state;
  $("sample-button").disabled = busy;
  $("refresh-button").disabled = busy;
  $("timeline-select").disabled = busy || !timelines.length;
  $("more-timelines").disabled = busy;
  $("earlier-events").disabled = busy;
  $("create-form").querySelector("button").disabled = busy;
  $("fork-button").disabled = busy || !available;
  for (const id of ["version-input", "version-slider"]) $(id).disabled = busy || !available;
  $("previous-button").disabled = busy || !available || version() <= 0;
  $("next-button").disabled = busy || !available || version() >= head();
  $("latest-button").disabled = busy || !available || isLatest();
  for (const id of ["action-kind", "action-item", "action-quantity", "action-button"]) $(id).disabled = busy || !readyToAct();
  $("delivery-quantity").disabled = busy || !selectedDelivery();
  $("delivery-button").disabled = busy || !selectedDelivery();
  $("merge-preview-button").disabled = busy || !readyToAct() || !$("merge-source").value;
  $("merge-apply-button").disabled = busy || !preview || stalePreview || !readyToAct() || preview.conflicts.some((conflict) => !resolutions.has(conflict.path));
  $("merge-source").disabled = busy || !timelines.length;
  $("merge-source-version").disabled = busy || !$("merge-source").value;
  for (const button of $("event-list").querySelectorAll("button")) button.disabled = busy;
  for (const input of $("merge-conflicts").querySelectorAll("input")) input.disabled = busy;
  document.querySelector(".workspace").setAttribute("aria-busy", String(busy));
}

async function action(work) {
  if (busy) return;
  setBusy(true);
  status("Working…");
  try { await work(); } catch (error) { showError(error); } finally { setBusy(false); }
}

function remember(item) {
  const index = timelines.findIndex((entry) => entry.id === item.id);
  if (index >= 0) timelines[index] = item;
  else timelines.unshift(item);
}

function relatedTimelines(currentId) {
  const byId = new Map(timelines.map((item) => [item.id, item]));
  function rootId(id) {
    const seen = new Set();
    while (byId.has(id) && !seen.has(id)) {
      seen.add(id);
      const item = byId.get(id);
      if (!item.parent_id) return id;
      id = item.parent_id;
    }
    return id;
  }
  const root = rootId(currentId);
  return timelines.filter((item) => item.id !== currentId && rootId(item.id) === root);
}

function renderTimelineOptions() {
  const current = timeline?.id || $("timeline-select").value;
  const incoming = $("merge-source").value;
  const related = relatedTimelines(current);
  for (const id of ["timeline-select", "merge-source"]) {
    const select = $(id);
    select.replaceChildren();
    const choices = id === "merge-source" ? related : timelines;
    if (!choices.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = id === "merge-source" ? "No related timelines" : "No timelines";
      select.append(option);
    }
    for (const item of choices) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = timelines.filter((entry) => entry.name === item.name).length > 1 ? `${item.name} (${item.id.slice(0, 6)})` : item.name;
      option.title = item.id;
      select.append(option);
    }
  }
  if (timelines.some((item) => item.id === current)) $("timeline-select").value = current;
  const preferred = related.find((item) => item.id === incoming) || related.find((item) => item.id === timeline?.parent_id) || related.find((item) => item.parent_id === current) || related[0];
  if (preferred) $("merge-source").value = preferred.id;
  $("timeline-count").textContent = timelines.length ? String(timelines.length) + (nextCursor ? "+" : "") : "";
  $("more-timelines").hidden = !nextCursor;
  updateSourceVersion();
}

async function loadTimelines(append = false) {
  const loaded = append ? [...timelines] : [];
  const known = new Set(loaded.map((item) => item.id));
  let cursor = append ? nextCursor : null;
  do {
    const query = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const result = await request(`/v1/timelines?limit=50${query}`);
    for (const item of result.items) if (!known.has(item.id)) { loaded.push(item); known.add(item.id); }
    cursor = result.next_cursor;
  } while (cursor);
  timelines = loaded;
  if (timeline && !timelines.some((item) => item.id === timeline.id)) timelines.push(timeline);
  nextCursor = null;
  renderTimelineOptions();
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function renderHistory() {
  const fragment = document.createDocumentFragment();
  const entries = timeline ? [{ version: 0, label: "Initial inventory", recorded_at: timeline.initial_at }, ...events] : [];
  for (const event of entries) {
    const wrapper = document.createElement("div");
    wrapper.setAttribute("role", "listitem");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "event-item";
    button.disabled = busy;
    button.setAttribute("aria-current", String(event.version === version()));
    button.title = `${event.label}\n${event.recorded_at}${event.inherited ? "\nInherited event" : ""}${event.merge ? "\nMerge event" : ""}`;
    const number = document.createElement("span");
    number.className = "event-version";
    number.textContent = `v${event.version}`;
    const detail = document.createElement("span");
    detail.className = "event-detail";
    const label = document.createElement("span");
    label.className = "event-label";
    label.textContent = event.label;
    const time = document.createElement("span");
    time.className = "event-time";
    time.textContent = dateLabel(event.recorded_at) + (event.inherited ? " / inherited" : "") + (event.merge ? " / merge" : "");
    detail.append(label, time);
    button.append(number, detail);
    button.addEventListener("click", () => navigate(event.version));
    wrapper.append(button);
    fragment.append(wrapper);
  }
  $("event-list").replaceChildren(fragment);
  $("earlier-events").hidden = !timeline || oldestVersion <= 1;
}

function renderState() {
  for (const metric of METRICS) $(metric.element).textContent = numberLabel(metricValue(state?.state, metric));
  $("workshop-name").textContent = timeline?.name || "Inventory";
  $("view-label").textContent = state ? (isLatest() ? `Latest / v${version()}` : `Past / v${version()}`) : "No workshop selected";
  $("view-label").title = timeline?.id || "";
  $("head-label").textContent = `/ ${head()}`;
  for (const id of ["version-input", "version-slider"]) { $(id).max = String(head()); $(id).value = String(version()); }
  $("state-time").textContent = state?.recorded_at || "";
  $("state-note").textContent = state && !isWorkshop(state.state) ? "This timeline is not a workshop." : state && !isLatest() ? "Past state. Actions are disabled." : "";
  $("action-target").textContent = timeline ? `Record in ${timeline.name}, latest version ${head()}` : "Select a workshop";
  $("fork-label").textContent = `Fork version ${version()}`;
  $("merge-target").textContent = timeline ? `Merge into ${timeline.name}, latest version ${head()}` : "Merge into the latest version of this workshop.";
  renderHistory();
  renderDelivery();
  updateControls();
}

function renderDelivery() {
  const delivery = selectedDelivery();
  $("delivery-target").textContent = delivery ? `Version ${delivery.event.version}: ${numberLabel(delivery.operation.value)} ${delivery.item} delivered` : "Select a delivery in history.";
  const selection = `${timeline?.id}:${version()}`;
  if (selection !== deliverySelection) {
    $("delivery-quantity").value = delivery ? String(delivery.operation.value) : "";
    deliverySelection = selection;
  }
}

function clearPreview() {
  preview = null;
  previewTarget = null;
  resolutions = new Map();
  stalePreview = false;
  $("merge-preview").hidden = true;
  $("merge-conflicts").replaceChildren();
  updateControls();
}

async function selectTimeline(id, requestedVersion) {
  const token = ++navigation;
  const result = await request(`/v1/timelines/${encodeURIComponent(id)}`);
  const after = Math.max(0, result.head_version - 50);
  const [eventResult, stateResult] = await Promise.all([
    request(`/v1/timelines/${encodeURIComponent(id)}/events?after_version=${after}&limit=50`),
    request(`/v1/timelines/${encodeURIComponent(id)}/state?version=${requestedVersion ?? result.head_version}`),
  ]);
  if (token !== navigation) return;
  timeline = result;
  state = stateResult;
  timeline.head_version = state.head_version;
  events = eventResult.items;
  oldestVersion = events[0]?.version || 0;
  remember(timeline);
  clearPreview();
  renderTimelineOptions();
  $("timeline-select").value = id;
  const parent = timelines.find((item) => item.id === timeline.parent_id);
  $("parent-info").hidden = !timeline.parent_id;
  $("parent-info").textContent = timeline.parent_id ? `From ${parent?.name || timeline.parent_id.slice(0, 8)}, version ${timeline.fork_version}` : "";
  $("fork-name").value = "";
  $("delivery-name").value = "";
  deliverySelection = "";
  await ensureEvent(version(), token);
  renderState();
  $("event-list").querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
}

async function ensureEvent(eventVersion, token) {
  if (!eventVersion || events.some((event) => event.version === eventVersion)) return;
  const result = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/events?after_version=${eventVersion - 1}&limit=1`);
  if (token !== navigation) return;
  if (result.items[0]?.version === eventVersion) { events.push(result.items[0]); events.sort((a, b) => a.version - b.version); }
}

async function navigate(value) {
  if (busy || !timeline) return;
  const next = Number(value);
  if (!Number.isSafeInteger(next) || next < 0 || next > head()) { renderState(); status(`Choose a version from 0 to ${head()}.`, true); return; }
  const token = ++navigation;
  const id = timeline.id;
  try {
    const result = await request(`/v1/timelines/${encodeURIComponent(id)}/state?version=${next}`);
    if (token !== navigation || timeline?.id !== id) return;
    state = result;
    timeline.head_version = result.head_version;
    clearPreview();
    await ensureEvent(next, token);
    if (token !== navigation) return;
    renderState();
    status("");
  } catch (error) { if (token === navigation) { renderState(); showError(error); } }
}

async function refresh() {
  const id = timeline?.id;
  await loadTimelines();
  if (id) await selectTimeline(id);
  else if (timelines.length) await selectTimeline(timelines[0].id);
  status("");
}

function switchTab(name) {
  activeTab = name;
  for (const tab of ["action", "delivery", "merge"]) {
    const selected = tab === name;
    $(`${tab}-tab`).setAttribute("aria-selected", String(selected));
    $(`${tab}-tab`).tabIndex = selected ? 0 : -1;
    $(`${tab}-panel`).hidden = !selected;
  }
  if (name === "delivery") renderDelivery();
}

function renderActionKind() {
  const kind = $("action-kind").value;
  $("action-item-label").hidden = kind === "build";
  $("action-button").textContent = kind === "build" ? "Build bicycles" : kind === "return" ? "Record return" : "Record delivery";
  $("action-hint").textContent = kind === "build" ? "Each bicycle uses 1 frame and 2 wheels." : kind === "return" ? "A return removes unused stock." : "A delivery adds stock.";
}

function updateSourceVersion() {
  const source = timelines.find((item) => item.id === $("merge-source").value);
  $("merge-source-version").max = String(source?.head_version ?? 0);
  $("merge-source-version").value = String(source?.head_version ?? 0);
  clearPreview();
}

function fieldLabel(path) {
  const metric = METRICS.find((item) => item.path === path);
  return metric?.label || (path === "/notes" ? "Notes" : path || "Whole inventory");
}

function conflictValue(entry) {
  if (!entry?.exists) return "missing";
  const value = entry.value;
  if (typeof value === "number") return numberLabel(value);
  return JSON.stringify(value, null, 2);
}

function mergedMetric(metric) {
  if (preview.merged_state) return metricValue(preview.merged_state, metric);
  const relevant = preview.conflicts.find((conflict) => conflict.path === metric.path || metric.path.startsWith(conflict.path + "/") || conflict.path === "");
  if (relevant) {
    const choice = resolutions.get(relevant.path);
    return choice ? metricValue(preview[choice].state, metric) : undefined;
  }
  const base = metricValue(preview.base.state, metric);
  const current = metricValue(preview.target.state, metric);
  const incoming = metricValue(preview.source.state, metric);
  return current === incoming ? current : current === base ? incoming : current;
}

function renderMergeTable() {
  const fragment = document.createDocumentFragment();
  for (const metric of METRICS) {
    const row = document.createElement("tr");
    const label = document.createElement("th");
    label.scope = "row";
    label.textContent = metric.label;
    row.append(label);
    for (const value of [metricValue(preview.target.state, metric), metricValue(preview.source.state, metric), mergedMetric(metric)]) {
      const cell = document.createElement("td");
      cell.textContent = numberLabel(value);
      row.append(cell);
    }
    fragment.append(row);
  }
  $("merge-stock").replaceChildren(fragment);
}

function renderPreview() {
  const incoming = timelines.find((item) => item.id === preview.source.timeline_id);
  $("merge-current-name").textContent = `Current: ${previewTarget.name}, v${preview.target.version}`;
  $("merge-incoming-name").textContent = `Incoming: ${incoming?.name || "Timeline"}, v${preview.source.version}`;
  $("merge-summary").textContent = preview.conflicts.length ? `${preview.conflicts.length} conflict${preview.conflicts.length === 1 ? "" : "s"}. Choose each result.` : "No conflicts.";
  const fragment = document.createDocumentFragment();
  for (const [index, conflict] of preview.conflicts.entries()) {
    const row = document.createElement("div");
    row.className = "conflict-row";
    const label = document.createElement("span");
    label.className = "conflict-path";
    label.textContent = fieldLabel(conflict.path);
    label.title = `Path: ${conflict.path || "/"}\nBase: ${conflictValue(conflict.base)}`;
    const options = document.createElement("div");
    options.className = "conflict-options";
    for (const choice of ["target", "source"]) {
      const option = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `conflict-${index}`;
      input.value = choice;
      input.checked = resolutions.get(conflict.path) === choice;
      input.addEventListener("change", () => { resolutions.set(conflict.path, choice); renderMergeTable(); updateControls(); });
      const detail = document.createElement("span");
      detail.className = "conflict-choice";
      const caption = document.createElement("span");
      caption.textContent = choice === "target" ? "Current" : "Incoming";
      const value = document.createElement("pre");
      value.className = "conflict-value";
      value.textContent = conflictValue(conflict[choice]);
      detail.append(caption, value);
      option.append(input, detail);
      options.append(option);
    }
    row.append(label, options);
    fragment.append(row);
  }
  $("merge-conflicts").replaceChildren(fragment);
  renderMergeTable();
  $("merge-preview").hidden = false;
  updateControls();
}

async function loadPreview() {
  if (!readyToAct()) throw new Error("Show the latest workshop version before merging.");
  const source = $("merge-source").value;
  if (!source) throw new Error("Choose an incoming timeline.");
  const sourceVersion = Number($("merge-source-version").value);
  const max = Number($("merge-source-version").max);
  if (!Number.isSafeInteger(sourceVersion) || sourceVersion < 0 || sourceVersion > max) throw new Error(`Choose an incoming version from 0 to ${max}.`);
  const result = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/merge-preview?source=${encodeURIComponent(source)}&source_version=${sourceVersion}`);
  preview = result;
  previewTarget = { id: timeline.id, name: timeline.name };
  resolutions = new Map();
  stalePreview = false;
  renderPreview();
  status(result.conflicts.length ? "Choose current or incoming for each conflicting value." : "Merge preview ready.");
}

$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const name = $("create-name").value.trim();
    if (!name) throw new Error("Enter a workshop name.");
    const result = await mutate("/v1/timelines", { name, initial_state: INITIAL_STATE });
    remember(result);
    await selectTimeline(result.id);
    $("create-name").value = "";
    $("new-workshop").open = false;
    status("Workshop created.");
  });
});
$("sample-button").addEventListener("click", () => action(async () => {
  const result = await mutate("/v1/examples/inventory", {});
  remember(result);
  await selectTimeline(result.id);
  status("Workshop sample loaded.");
}));
$("refresh-button").addEventListener("click", () => action(refresh));
$("retry-refresh").addEventListener("click", () => action(stalePreview ? loadPreview : refresh));
$("key-form").addEventListener("submit", (event) => { event.preventDefault(); action(refresh); });
$("timeline-select").addEventListener("change", () => action(async () => { await selectTimeline($("timeline-select").value); status(""); }));
$("more-timelines").addEventListener("click", () => action(async () => { await loadTimelines(true); status(""); }));
$("earlier-events").addEventListener("click", () => action(async () => {
  const after = Math.max(0, oldestVersion - 51);
  const result = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/events?after_version=${after}&limit=50`);
  const known = new Set(events.map((event) => event.version));
  events.push(...result.items.filter((event) => !known.has(event.version)));
  events.sort((a, b) => a.version - b.version);
  oldestVersion = events[0]?.version || 0;
  renderHistory();
  status("");
}));
$("version-slider").addEventListener("input", () => { $("version-input").value = $("version-slider").value; });
$("version-slider").addEventListener("change", () => navigate($("version-slider").value));
$("version-input").addEventListener("change", () => navigate($("version-input").value));
$("previous-button").addEventListener("click", () => navigate(version() - 1));
$("next-button").addEventListener("click", () => navigate(version() + 1));
$("latest-button").addEventListener("click", () => navigate(head()));
$("action-kind").addEventListener("change", renderActionKind);
$("action-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!readyToAct()) throw new Error("Show the latest workshop version before recording an action.");
    const kind = $("action-kind").value;
    const payload = { kind, quantity: positiveQuantity("action-quantity"), expected_version: head() };
    if (kind !== "build") payload.item = $("action-item").value;
    const result = await mutate(`/v1/workshop/${encodeURIComponent(timeline.id)}/actions`, payload);
    await selectTimeline(result.timeline.id);
    status(`Recorded ${result.event.label}.`);
  });
});
$("fork-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const name = $("fork-name").value.trim();
    if (!name) throw new Error("Enter a new timeline name.");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/fork`, { name, version: version() });
    remember(result);
    await selectTimeline(result.id);
    status("Independent workshop created.");
  });
});
$("delivery-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const delivery = selectedDelivery();
    if (!delivery) throw new Error("Select a stock delivery in history.");
    const name = $("delivery-name").value.trim();
    if (!name) throw new Error("Enter a new timeline name.");
    const quantity = positiveQuantity("delivery-quantity");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/experiment`, {
      name, event_version: delivery.event.version,
      label: `Delivery: ${quantity} ${delivery.item}`,
      operations: [{ op: "increment", path: delivery.operation.path, value: quantity }],
    });
    remember(result.timeline);
    await selectTimeline(result.timeline.id);
    switchTab("action");
    status(`Delivery changed. Replayed ${result.replayed_events} later events.`);
  });
});
$("merge-source").addEventListener("change", updateSourceVersion);
$("merge-source-version").addEventListener("change", clearPreview);
$("merge-preview-form").addEventListener("submit", (event) => { event.preventDefault(); action(loadPreview); });
$("merge-apply-button").addEventListener("click", () => action(async () => {
  if (!preview || stalePreview || previewTarget.id !== timeline?.id) throw new Error("Create a fresh merge preview first.");
  if (preview.conflicts.some((conflict) => !resolutions.has(conflict.path))) throw new Error("Choose a result for every conflict.");
  try {
    const result = await mutate(`/v1/timelines/${encodeURIComponent(previewTarget.id)}/merge`, {
      source_id: preview.source.timeline_id, source_version: preview.source.version,
      expected_version: preview.target.version,
      resolutions: preview.conflicts.map((conflict) => ({ path: conflict.path, choice: resolutions.get(conflict.path) })),
    });
    await selectTimeline(result.timeline.id);
    status("Merge recorded. Both histories are preserved.");
  } catch (error) {
    // keep a stale preview and its choices visible until explicitly refreshed.
    if (error.status === 409) stalePreview = true;
    throw error;
  }
}));

for (const name of ["action", "delivery", "merge"]) {
  $(`${name}-tab`).addEventListener("click", () => switchTab(name));
  $(`${name}-tab`).addEventListener("keydown", (event) => {
    const names = ["action", "delivery", "merge"];
    const index = names.indexOf(activeTab);
    const next = event.key === "ArrowRight" ? names[(index + 1) % names.length] : event.key === "ArrowLeft" ? names[(index + names.length - 1) % names.length] : null;
    if (next) { event.preventDefault(); switchTab(next); $(`${next}-tab`).focus(); }
  });
}

async function start() {
  setBusy(true);
  try {
    const health = await request("/health");
    apiKeyRequired = !!health.api_key_required;
    $("key-form").hidden = !apiKeyRequired;
    if (apiKeyRequired) { status("Enter the API key to connect."); $("api-key").focus(); }
    else await refresh();
  } catch (error) { showError(error); } finally { setBusy(false); }
}

renderActionKind();
start();
