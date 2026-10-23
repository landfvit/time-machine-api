"use strict";

const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries([
  "timeline-select", "timeline-count", "more-timelines", "parent-info", "event-list", "earlier-events",
  "state-json", "state-timestamp", "state-hash", "version-input", "version-slider", "head-label",
  "previous-button", "next-button", "head-button", "state-time", "fork-label", "append-target",
  "experiment-target", "compare-source", "compare-timeline", "compare-version", "compare-results",
  "status", "conflict-refresh", "sample-button", "refresh-button", "api-key", "key-form",
].map((id) => [id, $(id)]));

let timelines = [];
let nextCursor = null;
let timeline = null;
let selectedState = null;
let events = [];
let oldestVersion = 0;
let activeTab = "event";
let busy = false;
let stateRequest = 0;
let timelineRequest = 0;
let experimentVersion = null;
let apiKeyRequired = false;
let renderedTimeInput = "";
let mergePreview = null;
const retryKeys = new Map();

class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function status(message = "", isError = false) {
  ui.status.textContent = message;
  ui.status.parentElement.classList.toggle("error", isError);
  ui["conflict-refresh"].hidden = true;
}

function showError(error) {
  status(error.message || "Request failed.", true);
  if (error.status === 409) ui["conflict-refresh"].hidden = false;
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiKeyRequired && ui["api-key"].value) headers.set("X-API-Key", ui["api-key"].value);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError("Cannot reach the server. Retry when it is available.", 0, "network_error");
  }
  if (!response.ok) {
    let body;
    try { body = await response.json(); } catch { body = null; }
    validateNumbers(body);
    const detail = body?.error;
    let message = typeof detail?.message === "string" ? detail.message : `Request failed (${response.status}).`;
    const issues = detail?.details?.issues;
    if (Array.isArray(issues) && issues.length) {
      message += " " + issues.slice(0, 2).map((issue) => `${issue.location.join(".")}: ${issue.message}`).join(" ");
    }
    if (detail?.code === "replay_conflict" && typeof detail.details?.message === "string") {
      message += " " + detail.details.message;
    }
    throw new ApiError(message, response.status, detail?.code);
  }
  if (options.raw) return response;
  const result = await response.json();
  validateNumbers(result);
  return result;
}

function validateNumbers(value) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("The browser cannot represent this JSON number. Use a finite number or the API for this document.");
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) throw new Error("This number exceeds the browser's exact integer range. Use the API or JSON export for this document.");
  } else if (value !== null && typeof value === "object") {
    for (const entry of Object.values(value)) validateNumbers(entry);
  }
}

async function mutate(path, payload) {
  const body = JSON.stringify(payload);
  const signature = path + "\n" + body;
  // keep the same key when a response is lost and the user retries.
  if (!retryKeys.has(signature)) retryKeys.set(signature, crypto.randomUUID());
  try {
    const result = await request(path, { method: "POST", body, headers: { "Idempotency-Key": retryKeys.get(signature) } });
    retryKeys.delete(signature);
    return result;
  } catch (error) {
    if (error.status && error.status < 500) retryKeys.delete(signature);
    throw error;
  } finally {
    if (retryKeys.size > 30) retryKeys.delete(retryKeys.keys().next().value);
  }
}

function parseJSON(id, kind) {
  let value;
  try { value = JSON.parse($(id).value); } catch (error) { throw new Error(`Invalid JSON: ${error.message}`); }
  validateNumbers(value);
  if (kind === "object" && (value === null || Array.isArray(value) || typeof value !== "object")) throw new Error("Initial state must be a JSON object.");
  if (kind === "array" && !Array.isArray(value)) throw new Error("Operations must be a JSON array.");
  return value;
}

function json(value) { return JSON.stringify(value, null, 2); }
function currentVersion() { return selectedState?.version ?? 0; }
function headVersion() { return timeline?.head_version ?? 0; }
function selectedEvent() { return events.find((event) => event.version === currentVersion()); }

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function localDateInput(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 23);
}

function setBusy(value) {
  busy = value;
  updateControls();
}

function updateControls() {
  const available = !!timeline && !!selectedState;
  document.querySelector(".workspace").setAttribute("aria-busy", String(busy));
  ui["sample-button"].disabled = busy;
  ui["refresh-button"].disabled = busy;
  ui["timeline-select"].disabled = busy || !timelines.length;
  ui["more-timelines"].disabled = busy;
  ui["earlier-events"].disabled = busy;
  $("create-form").querySelector("button").disabled = busy;
  for (const id of ["fork-button", "append-button", "export-button"]) $(id).disabled = busy || !available;
  for (const id of ["version-input", "version-slider", "state-time"]) ui[id].disabled = busy || !available;
  $("time-form").querySelector("button").disabled = busy || !available;
  ui["previous-button"].disabled = busy || !available || currentVersion() <= 0;
  ui["next-button"].disabled = busy || !available || currentVersion() >= headVersion();
  ui["head-button"].disabled = busy || !available || currentVersion() === headVersion();
  $("experiment-button").disabled = busy || !available || currentVersion() === 0;
  $("compare-button").disabled = busy || !available || !ui["compare-timeline"].value;
  ui["compare-timeline"].disabled = busy || !timelines.length;
  ui["compare-version"].disabled = busy || !timelines.length;
  for (const button of ui["event-list"].querySelectorAll("button")) button.disabled = busy;
  const mergeReady = available && currentVersion() === headVersion() && !!$("merge-source").value;
  $("merge-source").disabled = busy || !available;
  $("merge-preview-button").disabled = busy || !mergeReady;
  $("merge-apply-button").disabled = busy || !mergeReady || !mergePreview || [...$("merge-conflicts").querySelectorAll("select")].some((select) => !select.value);
  for (const select of $("merge-conflicts").querySelectorAll("select")) select.disabled = busy;
}

async function action(work) {
  if (busy) return;
  setBusy(true);
  status("Working…");
  try { await work(); } catch (error) { showError(error); } finally { setBusy(false); }
}

function renderTimelineOptions() {
  const selected = timeline?.id || ui["timeline-select"].value;
  const compareSelected = ui["compare-timeline"].value;
  for (const select of [ui["timeline-select"], ui["compare-timeline"]]) {
    select.replaceChildren();
    if (!timelines.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No timelines";
      select.append(option);
    }
    for (const item of timelines) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = timelines.filter((entry) => entry.name === item.name).length > 1 ? `${item.name} (${item.id.slice(0, 6)})` : item.name;
      option.title = item.id;
      select.append(option);
    }
  }
  if (timelines.some((item) => item.id === selected)) ui["timeline-select"].value = selected;
  const other = timelines.find((item) => item.id === compareSelected) || timelines.find((item) => item.id !== selected) || timelines[0];
  if (other) ui["compare-timeline"].value = other.id;
  ui["timeline-count"].textContent = timelines.length ? String(timelines.length) + (nextCursor ? "+" : "") : "";
  ui["more-timelines"].hidden = !nextCursor;
  updateCompareVersion();
  const incoming = $("merge-source").value;
  $("merge-source").replaceChildren();
  const root = timelineRoot(selected);
  for (const item of timelines.filter((entry) => entry.id !== selected && (!root || !timelineRoot(entry.id) || timelineRoot(entry.id) === root))) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    option.title = item.id;
    $("merge-source").append(option);
  }
  if (!$("merge-source").options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No other timelines";
    $("merge-source").append(option);
  } else if ([...$("merge-source").options].some((option) => option.value === incoming)) $("merge-source").value = incoming;
  invalidateMerge();
}

function timelineRoot(id) {
  const visited = new Set();
  let item = timelines.find((entry) => entry.id === id);
  while (item?.parent_id && !visited.has(item.id)) {
    visited.add(item.id);
    item = timelines.find((entry) => entry.id === item.parent_id);
  }
  return item && !item.parent_id ? item.id : null;
}

function rememberTimeline(item) {
  const index = timelines.findIndex((entry) => entry.id === item.id);
  if (index >= 0) timelines[index] = item;
  else timelines.unshift(item);
}

async function loadTimelineList(append = false) {
  const cursor = append && nextCursor ? `&cursor=${encodeURIComponent(nextCursor)}` : "";
  const result = await request(`/v1/timelines?limit=50${cursor}`);
  if (append) {
    const known = new Set(timelines.map((item) => item.id));
    timelines.push(...result.items.filter((item) => !known.has(item.id)));
  } else {
    timelines = result.items;
    if (timeline && !timelines.some((item) => item.id === timeline.id)) timelines.push(timeline);
  }
  nextCursor = result.next_cursor;
  renderTimelineOptions();
}

function renderEvents() {
  const fragment = document.createDocumentFragment();
  const initial = { version: 0, label: "Initial state", recorded_at: timeline?.initial_at || timeline?.created_at };
  const entries = timeline ? [initial, ...events] : [];
  for (const event of entries) {
    const wrapper = document.createElement("div");
    wrapper.setAttribute("role", "listitem");
    const button = document.createElement("button");
    button.type = "button";
    button.disabled = busy;
    button.className = "event-item";
    button.dataset.version = String(event.version);
    button.setAttribute("aria-current", String(event.version === currentVersion()));
    button.title = `${event.label || "Event"}\n${event.recorded_at || ""}${event.inherited ? "\nInherited event" : ""}`;
    const number = document.createElement("span");
    number.className = "event-version";
    number.textContent = `v${event.version}`;
    const detail = document.createElement("span");
    detail.className = "event-detail";
    const label = document.createElement("span");
    label.className = "event-label";
    label.textContent = event.label || "Event";
    const time = document.createElement("span");
    time.className = "event-time";
    time.textContent = dateLabel(event.recorded_at || "") + (event.inherited ? " / inherited" : "") + (event.merge ? " / merge" : "");
    detail.append(label, time);
    button.append(number, detail);
    button.addEventListener("click", () => { if (!busy) navigateVersion(event.version); });
    wrapper.append(button);
    fragment.append(wrapper);
  }
  ui["event-list"].replaceChildren(fragment);
  ui["earlier-events"].hidden = !timeline || oldestVersion <= 1;
}

function renderState() {
  const version = currentVersion();
  const head = headVersion();
  ui["state-json"].textContent = selectedState ? json(selectedState.state) : "{}";
  ui["state-timestamp"].textContent = selectedState?.recorded_at || "";
  ui["state-timestamp"].title = selectedState?.recorded_at || "Recorded time in UTC";
  const hash = selectedState?.state_hash || "";
  ui["state-hash"].textContent = hash ? hash.slice(0, 10) : "";
  ui["state-hash"].title = hash ? `SHA-256: ${hash}` : "SHA-256 state hash";
  for (const id of ["version-input", "version-slider"]) {
    ui[id].max = String(head);
    ui[id].value = String(version);
  }
  ui["head-label"].textContent = `/ ${head}`;
  ui["state-time"].value = selectedState ? localDateInput(selectedState.recorded_at) : "";
  renderedTimeInput = ui["state-time"].value;
  ui["fork-label"].textContent = `Fork version ${version}`;
  ui["append-target"].textContent = timeline ? `Append to latest: version ${head}` : "Append to version 0";
  ui["compare-source"].textContent = timeline ? `${timeline.name}, version ${version}` : "Compare selected state with";
  ui["compare-results"].hidden = true;
  $("merge-target").textContent = timeline ? `Merge into ${timeline.name}, latest version ${head}.` : "Select the latest version to merge.";
  if (timeline && version !== head) $("merge-target").textContent += " Select Latest first.";
  invalidateMerge();
  renderEvents();
  populateExperiment();
  updateControls();
}

function populateExperiment() {
  const event = selectedEvent();
  const version = currentVersion();
  ui["experiment-target"].textContent = version ? `Replace event ${version}, replay through ${headVersion()}` : "Select an event in history";
  if (experimentVersion !== `${timeline?.id}:${version}`) {
    $("experiment-label").value = event?.label || "";
    $("experiment-operations").value = json(event?.operations || []);
    experimentVersion = `${timeline?.id}:${version}`;
  }
}

async function ensureSelectedEvent(version, id, token) {
  if (!version || events.some((event) => event.version === version)) return;
  const result = await request(`/v1/timelines/${encodeURIComponent(id)}/events?after_version=${version - 1}&limit=1`);
  if (token !== stateRequest || timeline?.id !== id) return;
  if (result.items[0]?.version === version) {
    events.push(result.items[0]);
    events.sort((a, b) => a.version - b.version);
    experimentVersion = null;
    populateExperiment();
    renderEvents();
  }
}

async function loadState(query, silent = false) {
  if (!timeline) return;
  const token = ++stateRequest;
  const id = timeline.id;
  try {
    const result = await request(`/v1/timelines/${encodeURIComponent(id)}/state?${query}`);
    // late requests must not replace a more recently selected version.
    if (token !== stateRequest || timeline?.id !== id) return;
    selectedState = result;
    timeline.head_version = result.head_version;
    renderState();
    if (!silent) status("");
    await ensureSelectedEvent(result.version, id, token);
  } catch (error) {
    if (token === stateRequest) { renderState(); throw error; }
  }
}

async function navigateVersion(version) {
  if (!timeline || busy) return;
  const parsed = Number(version);
  if (!Number.isInteger(parsed) || parsed < 0 || parsed > headVersion()) {
    renderState();
    status(`Choose a version from 0 to ${headVersion()}.`, true);
    return;
  }
  try { await loadState(`version=${parsed}`); } catch (error) { showError(error); }
}

async function selectTimeline(id, version) {
  const token = ++timelineRequest;
  ++stateRequest;
  const result = await request(`/v1/timelines/${encodeURIComponent(id)}`);
  const after = Math.max(0, result.head_version - 50);
  const eventResult = await request(`/v1/timelines/${encodeURIComponent(id)}/events?after_version=${after}&limit=50`);
  if (token !== timelineRequest) return;
  timeline = result;
  rememberTimeline(result);
  events = eventResult.items;
  oldestVersion = events[0]?.version || 0;
  selectedState = null;
  experimentVersion = null;
  renderTimelineOptions();
  ui["timeline-select"].value = id;
  ui["parent-info"].hidden = !result.parent_id;
  const parent = timelines.find((item) => item.id === result.parent_id);
  ui["parent-info"].textContent = result.parent_id ? `From ${parent?.name || result.parent_id.slice(0, 8)}, version ${result.fork_version}` : "";
  $("fork-name").value = "";
  $("experiment-name").value = "";
  await loadState(`version=${version ?? result.head_version}`, true);
  ui["event-list"].querySelector('[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
}

async function refresh() {
  const id = timeline?.id;
  await loadTimelineList();
  if (id && timelines.some((item) => item.id === id)) await selectTimeline(id);
  else if (timelines.length) await selectTimeline(timelines[0].id);
  status("");
}

function switchTab(name) {
  activeTab = name;
  for (const tab of ["event", "experiment", "compare", "merge"]) {
    const selected = tab === name;
    $(`${tab}-tab`).setAttribute("aria-selected", String(selected));
    $(`${tab}-tab`).tabIndex = selected ? 0 : -1;
    $(`${tab}-panel`).hidden = !selected;
  }
  if (name === "experiment") populateExperiment();
}

function invalidateMerge() {
  mergePreview = null;
  $("merge-apply-form").hidden = true;
  $("merge-conflicts").replaceChildren();
  $("merge-state").hidden = true;
  updateControls();
}

function mergeValue(value) {
  return value.exists ? json(value.value) : "(removed)";
}

function renderMergePreview(result) {
  mergePreview = result;
  const source = timelines.find((item) => item.id === result.source.timeline_id);
  const base = timelines.find((item) => item.id === result.base.timeline_id);
  $("merge-base").textContent = `Incoming: ${source?.name || result.source.timeline_id}, v${result.source.version}. Shared base: ${base?.name || result.base.timeline_id}, v${result.base.version}.`;
  $("merge-conflicts").replaceChildren();
  for (const conflict of result.conflicts) {
    const fieldset = document.createElement("fieldset");
    fieldset.className = "merge-conflict";
    const legend = document.createElement("legend");
    legend.textContent = conflict.path || "Document root";
    const values = document.createElement("div");
    values.className = "merge-values";
    for (const [name, value] of [["Current", conflict.target], ["Incoming", conflict.source]]) {
      const column = document.createElement("div");
      const label = document.createElement("span");
      label.textContent = name;
      const pre = document.createElement("pre");
      pre.textContent = mergeValue(value);
      column.append(label, pre);
      values.append(column);
    }
    const shared = document.createElement("p");
    shared.className = "metadata";
    shared.textContent = `Shared base: ${mergeValue(conflict.base)}`;
    const label = document.createElement("label");
    label.textContent = "Keep value";
    const select = document.createElement("select");
    select.dataset.path = conflict.path;
    select.setAttribute("aria-label", `Resolve ${conflict.path || "document root"}`);
    for (const [value, text] of [["", "Choose a value"], ["target", "Current timeline"], ["source", "Incoming timeline"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.append(option);
    }
    select.addEventListener("change", updateControls);
    label.append(select);
    fieldset.append(legend, values, shared, label);
    $("merge-conflicts").append(fieldset);
  }
  $("merge-state").hidden = result.merged_state === null;
  $("merge-state").textContent = result.merged_state === null ? "" : json(result.merged_state);
  $("merge-apply-form").hidden = false;
  updateControls();
}

function updateCompareVersion() {
  const other = timelines.find((item) => item.id === ui["compare-timeline"].value);
  ui["compare-version"].max = String(other?.head_version ?? 0);
  ui["compare-version"].value = String(other?.head_version ?? 0);
  ui["compare-results"].hidden = true;
  updateControls();
}

$("create-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const name = $("create-name").value.trim();
    if (!name) throw new Error("Enter a timeline name.");
    const result = await mutate("/v1/timelines", { name, initial_state: parseJSON("initial-state", "object") });
    rememberTimeline(result);
    await selectTimeline(result.id);
    $("create-name").value = "";
    $("create-form").closest("details").open = false;
    status("Timeline created.");
  });
});

ui["sample-button"].addEventListener("click", () => action(async () => {
  const result = await mutate("/v1/examples/inventory", {});
  rememberTimeline(result);
  await selectTimeline(result.id);
  status("Workshop inventory loaded. Select an event to explore its state.");
}));

ui["refresh-button"].addEventListener("click", () => action(refresh));
ui["conflict-refresh"].addEventListener("click", () => action(refresh));
ui["key-form"].addEventListener("submit", (event) => { event.preventDefault(); action(refresh); });
ui["timeline-select"].addEventListener("change", () => action(async () => {
  await selectTimeline(ui["timeline-select"].value);
  status("");
}));
ui["more-timelines"].addEventListener("click", () => action(async () => { await loadTimelineList(true); status(""); }));
ui["earlier-events"].addEventListener("click", () => action(async () => {
  const after = Math.max(0, oldestVersion - 51);
  const result = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/events?after_version=${after}&limit=50`);
  const known = new Set(events.map((event) => event.version));
  events.push(...result.items.filter((event) => !known.has(event.version)));
  events.sort((a, b) => a.version - b.version);
  oldestVersion = events[0]?.version || 0;
  renderEvents();
  status("");
}));

ui["version-slider"].addEventListener("input", () => { ui["version-input"].value = ui["version-slider"].value; });
ui["version-slider"].addEventListener("change", () => navigateVersion(ui["version-slider"].value));
ui["version-input"].addEventListener("change", () => navigateVersion(ui["version-input"].value));
ui["previous-button"].addEventListener("click", () => navigateVersion(currentVersion() - 1));
ui["next-button"].addEventListener("click", () => navigateVersion(currentVersion() + 1));
ui["head-button"].addEventListener("click", () => navigateVersion(headVersion()));
$("time-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const date = new Date(ui["state-time"].value);
    if (Number.isNaN(date.getTime())) throw new Error("Choose a valid local date and time.");
    // preserve sub-millisecond precision when querying the displayed timestamp.
    const at = ui["state-time"].value === renderedTimeInput ? selectedState.recorded_at : date.toISOString();
    await loadState(`at=${encodeURIComponent(at)}`);
  });
});

$("event-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline) return;
    const label = $("event-label").value.trim();
    if (!label) throw new Error("Enter an event label.");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/events`, {
      label, operations: parseJSON("event-operations", "array"), expected_version: headVersion(),
    });
    await selectTimeline(result.timeline.id);
    status(`Appended version ${result.event.version}.`);
  });
});

$("fork-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline) return;
    const name = $("fork-name").value.trim();
    if (!name) throw new Error("Enter a new timeline name.");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/fork`, { name, version: currentVersion() });
    rememberTimeline(result);
    await selectTimeline(result.id);
    status("Independent timeline created.");
  });
});

$("experiment-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline || !currentVersion()) throw new Error("Select an event in history first.");
    const name = $("experiment-name").value.trim();
    const label = $("experiment-label").value.trim();
    if (!name || !label) throw new Error("Enter a timeline name and event label.");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/experiment`, {
      name, event_version: currentVersion(), operations: parseJSON("experiment-operations", "array"), label,
    });
    rememberTimeline(result.timeline);
    await selectTimeline(result.timeline.id);
    switchTab("compare");
    status(`Experiment created. Replayed ${result.replayed_events} later events.`);
  });
});

ui["compare-timeline"].addEventListener("change", updateCompareVersion);
$("compare-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline || !selectedState) return;
    const version = Number(ui["compare-version"].value);
    const max = Number(ui["compare-version"].max);
    if (!Number.isInteger(version) || version < 0 || version > max) throw new Error(`Choose a comparison version from 0 to ${max}.`);
    const params = new URLSearchParams({ left: timeline.id, right: ui["compare-timeline"].value, left_version: currentVersion(), right_version: version });
    const result = await request(`/v1/compare?${params}`);
    $("compare-changes").textContent = result.changes.length ? json(result.changes) : "[]";
    $("change-count").textContent = `${result.changes.length} change${result.changes.length === 1 ? "" : "s"}`;
    $("compare-state").textContent = json(result.right.state);
    const other = timelines.find((item) => item.id === ui["compare-timeline"].value);
    $("compare-state-label").textContent = `${other?.name || "Other state"}, version ${result.right.version}`;
    ui["compare-results"].hidden = false;
    status(result.changes.length ? "Differences transform the selected state into the other state." : "States are identical.");
  });
});

$("export-button").addEventListener("click", () => action(async () => {
  if (!timeline || !selectedState) return;
  const response = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/export?version=${currentVersion()}`, { raw: true });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `timeline-${timeline.id}-v${currentVersion()}.json`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  status("JSON downloaded.");
}));

$("merge-source").addEventListener("change", invalidateMerge);
$("merge-preview-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline || !selectedState || currentVersion() !== headVersion()) throw new Error("Select the latest version before merging.");
    invalidateMerge();
    const params = new URLSearchParams({ source: $("merge-source").value });
    const result = await request(`/v1/timelines/${encodeURIComponent(timeline.id)}/merge-preview?${params}`);
    if (result.target.version !== headVersion()) throw new ApiError("The timeline changed. Refresh latest before previewing a merge.", 409, "version_conflict");
    renderMergePreview(result);
    status(result.conflicts.length ? `Resolve ${result.conflicts.length} conflict${result.conflicts.length === 1 ? "" : "s"} before merging.` : "No conflicts. Review the merged state, then apply.");
  });
});
$("merge-apply-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    if (!timeline || !mergePreview || mergePreview.target.timeline_id !== timeline.id) throw new Error("Preview the merge first.");
    const resolutions = [...$("merge-conflicts").querySelectorAll("select")].map((select) => ({ path: select.dataset.path, choice: select.value }));
    if (resolutions.some((resolution) => !resolution.choice)) throw new Error("Choose a value for every conflict.");
    const result = await mutate(`/v1/timelines/${encodeURIComponent(timeline.id)}/merge`, {
      source_id: mergePreview.source.timeline_id,
      source_version: mergePreview.source.version,
      expected_version: mergePreview.target.version,
      resolutions,
    });
    await selectTimeline(result.timeline.id);
    status(`Merged into version ${result.event.version}. The incoming timeline is unchanged.`);
  });
});

for (const name of ["event", "experiment", "compare", "merge"]) {
  $(`${name}-tab`).addEventListener("click", () => switchTab(name));
  $(`${name}-tab`).addEventListener("keydown", (event) => {
    const names = ["event", "experiment", "compare", "merge"];
    const index = names.indexOf(activeTab);
    const next = event.key === "ArrowRight" ? names[(index + 1) % names.length] : event.key === "ArrowLeft" ? names[(index + names.length - 1) % names.length] : null;
    if (!next) return;
    event.preventDefault();
    switchTab(next);
    $(`${next}-tab`).focus();
  });
}

async function start() {
  setBusy(true);
  try {
    const health = await request("/health");
    apiKeyRequired = !!health.api_key_required;
    ui["key-form"].hidden = !apiKeyRequired;
    if (apiKeyRequired) {
      status("Enter the API key to connect.");
      ui["api-key"].focus();
    } else await refresh();
  } catch (error) { showError(error); } finally { setBusy(false); }
}

start();
