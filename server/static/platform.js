import { createBoardMap } from "./map.js";
import { barChart, lineChart } from "./charts.js";
import { actionCard, needsAction, renderKeyed, situationChips, startClock } from "./actions.js";
import { createAlarm } from "./alarm.js";
import { escalationNotes } from "./escalation.js";
import { ApiError, api, el, clockTime, dateTime, durationText, hazardText, listNames, minutesSince, plural, secondsAgo, stateOf, uptimeText, withButton } from "./common.js";

const BOARD_REFRESH_MS = 2000;
const MAP_CENTER = [31.3, -89.31]; // Pine Belt, Mississippi
const STATE_LABEL = { out: "Power out", acked: "Power out, responding", on: "Power on", quiet: "No signal" };
const ROLES = ["Family", "Caregiver", "Home health nurse", "Agency on-call", "County emergency management", "Other"];

// ---- session -----------------------------------------------------------------

async function guarded(promise) {
  try {
    return await promise;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) location.assign("/login");
    throw error;
  }
}

document.getElementById("signout").addEventListener("click", async () => {
  await api("POST", "/api/logout", {});
  location.assign("/login");
});
// The server enforces every permission; the page only hides what this role can't use.
const me = await guarded(api("GET", "/api/me"));
const can = (permission) => me.permissions.includes(permission);
document.getElementById("who").textContent = me.email;
document.getElementById("role").textContent = me.role_label;

// ---- views ---------------------------------------------------------------------

const VIEW_NEEDS = { board: "view_board", patients: "view_patients", history: "view_history", units: "view_board", staff: "manage_staff" };
const views = Object.fromEntries(Object.keys(VIEW_NEEDS).filter((name) => can(VIEW_NEEDS[name]))
  .map((name) => [name, document.getElementById(`view-${name}`)]));
for (const link of document.querySelectorAll("[data-view]")) link.hidden = !(link.dataset.view in views);
document.getElementById("county-link").hidden = !can("view_county");
document.getElementById("add-patient").hidden = !can("manage_patients");

function currentView() {
  const name = location.hash.slice(1);
  return name in views ? name : "board";
}

function showView() {
  const name = currentView();
  for (const [key, section] of Object.entries(views)) section.hidden = key !== name;
  for (const link of document.querySelectorAll("[data-view]")) {
    if (link.dataset.view === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (name === "patients") loadRoster();
  else if (name === "history") loadHistory();
  else if (name === "units") renderUnits();
  else if (name === "staff") loadStaff();
  else boardMap.resize();
}
window.addEventListener("hashchange", showView);

// ---- live board ----------------------------------------------------------------

// The board's words for a unit: which emergency, if any, and whether someone has it.
const EMERGENCY_LABEL = { power: "Power out", machine: "Machine stopped", help: "Help requested" };
function labelFor(unit) {
  const state = stateOf(unit);
  if (unit.status !== "outage") return STATE_LABEL[state];
  const what = EMERGENCY_LABEL[unit.emergency] ?? "Power out";
  return unit.acknowledged_by ? `${what}, responding` : what;
}

// A chip or a name was tapped: bring that home's card into view and show it on the map.
function jumpTo(unit) {
  const card = document.querySelector(`#units [data-key="${CSS.escape(unit.device_id)}"]`);
  card?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  card?.querySelector("button, a")?.focus({ preventScroll: true });
  boardMap.focus(unit);
}

function headline(units) {
  const out = units.filter((u) => u.status === "outage");
  const waiting = out.filter((u) => !u.acknowledged_by);
  const node = document.getElementById("headline");
  node.classList.toggle("is-alarm", waiting.length > 0);
  if (!units.length) {
    node.replaceChildren(el("h1", {}, "No patients yet"), el("p", {}, "Add a patient to start monitoring their power."));
  } else if (!out.length) {
    node.replaceChildren(el("h1", {}, `All ${plural(units.length, "home")} have power`),
      situationChips(units, jumpTo) ?? el("p", {}, "We'll alert the call list the moment that changes."));
  } else {
    const allPower = out.every((u) => (u.emergency ?? "power") === "power");
    node.replaceChildren(el("h1", {}, allPower ? `${plural(out.length, "home")} without power` : `${plural(out.length, "home")} need help now`),
      situationChips(units, jumpTo));
  }
}

// One line per hazard, naming the patients in its path; high risk first (the server sorts each unit's list).
function stormBanner(units) {
  const byHazard = new Map();
  for (const unit of units) {
    for (const hazard of unit.hazards) {
      const key = `${hazard.level}|${hazardText(hazard)}`;
      if (!byHazard.has(key)) byHazard.set(key, { hazard, names: [] });
      byHazard.get(key).names.push(unit.name);
    }
  }
  const node = document.getElementById("storm-banner");
  node.hidden = byHazard.size === 0;
  if (!byHazard.size) return;
  const lines = [...byHazard.values()].sort((a, b) => (a.hazard.level === "high" ? -1 : 1) - (b.hazard.level === "high" ? -1 : 1));
  node.replaceChildren(
    el("h2", {}, "Storm watch"),
    el("ul", {}, lines.map(({ hazard, names }) => el("li", { class: `is-${hazard.level}` },
      el("strong", {}, hazardText(hazard)), el("span", { class: "visually-hidden" }, ": "), listNames(names)))),
    callFirst(units) ?? "",
    el("p", { class: "muted" }, "High-risk warnings have been sent to each patient's first contact. Check backups now, most critical first."),
  );
}

const CALL_FIRST_WITHIN_MINUTES = 240;  // four hours of backup or less

// In the storm's path and least able to wait: most critical first, then shortest backup.
function callFirst(units) {
  const urgent = units
    .filter((u) => u.hazards.length && u.status !== "outage" && u.backup_minutes <= CALL_FIRST_WITHIN_MINUTES)
    .sort((a, b) => a.acuity - b.acuity || a.backup_minutes - b.backup_minutes);
  if (!urgent.length) return null;
  // Care staff get a call link per patient (first person on the call list), so storm prep is one tap each.
  return el("div", { class: "call-first" }, el("strong", {}, "Call first"),
    el("ol", {}, urgent.map((u) => el("li", {},
      `${u.name}, ${durationText(u.backup_minutes)} of backup`,
      u.contacts?.length ? el("a", { class: "btn btn-call btn-small", href: `tel:${u.contacts[0].phone}` }, `Call ${u.contacts[0].name}`) : null))));
}

// Setup problems that would slow help down, grouped so ten never-seen units read as one line.
function attentionPanel(units) {
  const byProblem = new Map();
  for (const unit of units) {
    for (const problem of unit.attention) {
      if (!byProblem.has(problem)) byProblem.set(problem, []);
      byProblem.get(problem).push(unit.name);
    }
  }
  const node = document.getElementById("attention");
  node.hidden = byProblem.size === 0;
  if (!byProblem.size) return;
  const patients = new Set(units.filter((u) => u.attention.length).map((u) => u.name)).size;
  node.replaceChildren(
    el("summary", {}, `${plural(patients, "patient")} to check before the next outage`),
    el("ul", {}, [...byProblem].map(([problem, names]) => el("li", {}, el("strong", {}, problem), `: ${listNames(names)}`))),
  );
}

// Screen readers hear the headline only when it changes, not on every poll.
function announce(text) {
  const region = document.getElementById("announce");
  if (region.textContent !== text) region.textContent = text;
}

function unitRow(unit) {
  const state = stateOf(unit);
  const lines = [
    el("div", { class: "unit-head" },
      el("span", { class: "state-dot", "aria-hidden": "true" }),
      el("h2", {}, unit.name),
      el("span", { class: "unit-state" }, labelFor(unit))),
    el("p", { class: "muted" }, `${unit.equipment}, acuity ${unit.acuity}. ${unit.address}`),
  ];
  if (unit.status === "outage" && unit.emergency === "help") {
    lines.push(el("p", {}, `Help button pressed at ${clockTime(unit.outage_started)}. ` +
      (unit.contacts_alerted.length ? `Alerted ${listNames(unit.contacts_alerted)}.` : "")));
    if (unit.acknowledged_by) lines.push(el("p", {}, `Responding: ${unit.acknowledged_by}`));
    else lines.push(respondButton(unit));
  } else if (unit.status === "outage") {
    lines.push(
      backupLine(unit.backup_minutes_left),
      el("p", {}, `Out since ${clockTime(unit.outage_started)}${minutesSince(unit.outage_started) ? ` (${plural(minutesSince(unit.outage_started), "minute")})` : ", just now"}. ` +
        (unit.contacts_alerted.length ? `Alerted ${listNames(unit.contacts_alerted)}.` : "")),
    );
    if (unit.acknowledged_by) lines.push(el("p", {}, `Responding: ${unit.acknowledged_by}`));
    else lines.push(respondButton(unit));
  } else if (unit.status === "offline") {
    lines.push(el("p", {}, `Unit silent for ${durationText(minutesSince(unit.last_seen))}. Its Wi-Fi or battery may be down; check on the patient.` +
      (unit.silent_alert_sent ? " Care staff have been alerted." : "")));
  } else if (unit.status === "never_seen") {
    lines.push(el("p", { class: "muted" }, "Unit has not connected yet."));
  } else {
    lines.push(el("p", { class: "muted" }, `Last report ${secondsAgo(unit.last_seen)}${unit.battery_percent == null ? "" : `, unit battery ${unit.battery_percent}%`}.`));
  }
  lines.push(...escalationNotes(unit));
  for (const hazard of unit.hazards) {
    lines.push(el("p", { class: "unit-hazard" }, el("span", { class: "tag tag-storm" }, "Storm watch"), ` ${hazardText(hazard)}`));
  }
  return el("article", { class: `unit is-${state}` }, lines);
}

function backupLine(minutesLeft) {
  if (minutesLeft === 0) return el("p", { class: "unit-backup" }, el("strong", {}, "0"), "Backup has probably run out. Get someone there now.");
  const runsOut = clockTime(new Date(Date.now() + minutesLeft * 60000).toISOString());
  return el("p", { class: "unit-backup" }, el("strong", {}, String(minutesLeft)), `minutes of backup left, runs out around ${runsOut}`);
}

function respondButton(unit) {
  if (!can("respond")) return el("p", { class: "muted" }, "Waiting for someone on the call list to respond.");
  const button = el("button", { class: "btn btn-respond", type: "button" }, "I'm responding");
  button.addEventListener("click", () =>
    withButton(button, () => guarded(api("POST", `/api/ack/${encodeURIComponent(unit.device_id)}`, {}))).then(refreshBoard));
  return button;
}

const alarm = createAlarm(document.getElementById("sound-toggle"));
const boardMap = createBoardMap(document.getElementById("map"), { center: MAP_CENTER, labelFor });
const WEATHER_REFRESH_MS = 60000;

async function refreshWeather() {
  try {
    boardMap.showWeather(await guarded(api("GET", "/api/weather-map")));
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) boardMap.weatherUnavailable();
  }
}

let lastBoard = "";
let lastUnits = [];

async function refreshBoard() {
  try {
    const { units } = await guarded(api("GET", "/api/status"));
    const snapshot = JSON.stringify(units);
    // Rebuilding unchanged rows every poll would swallow clicks on "We're responding".
    if (snapshot === lastBoard) return;
    lastBoard = snapshot;
    lastUnits = units;
    alarm.notice(units);
    if (currentView() === "units") renderUnits();
    headline(units);
    stormBanner(units);
    attentionPanel(units);
    announce(document.querySelector("#headline h1").textContent);
    renderKeyed(document.getElementById("units"), units,
      (unit) => (needsAction(unit) ? actionCard(unit, { respondButton, onLocate: (u) => boardMap.focus(u) }) : unitRow(unit)));
    boardMap.showUnits(units);
  } catch (error) {
    lastBoard = "";  // force a full redraw once the server answers again
    if (!(error instanceof ApiError && error.status === 401)) {
      document.getElementById("headline").replaceChildren(el("h1", {}, "Can't reach the server"), el("p", {}, "Retrying every few seconds."));
    }
  }
}

// ---- roster --------------------------------------------------------------------

let roster = [];

async function loadRoster() {
  const body = document.getElementById("roster-body");
  try {
    roster = (await guarded(api("GET", "/api/patients"))).patients;
  } catch (error) {
    body.replaceChildren(el("p", { class: "form-error" }, error.message));
    return;
  }
  if (!roster.length) {
    body.replaceChildren(el("div", { class: "empty" },
      el("h2", {}, "No patients yet"),
      el("p", { class: "muted" }, can("manage_patients") ? "Add the first patient, then put their unit ID and token on the power unit."
                                                         : "An admin adds patients; they will appear here."),
      can("manage_patients") ? el("button", { class: "btn", type: "button", onclick: () => openForm() }, "Add patient") : null));
    return;
  }
  body.replaceChildren(el("table", {},
    el("thead", {}, el("tr", {}, ["Patient", "Equipment", "Acuity", "Call list", "Unit", ""].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, roster.map((p) => el("tr", {},
      el("td", {}, el("strong", {}, p.name), el("div", { class: "muted" }, p.address)),
      el("td", { class: "col-hide-mobile" }, p.equipment),
      el("td", { class: "col-hide-mobile" }, el("span", { class: `acuity acuity-${p.acuity}` }, String(p.acuity))),
      el("td", { class: "col-hide-mobile" }, p.contacts.map((c) => c.name).join(", ")),
      el("td", { class: "col-hide-mobile code" }, p.device_id),
      el("td", {}, can("manage_patients")
        ? el("button", { class: "btn btn-quiet btn-small", type: "button", onclick: () => openForm(p) }, "Edit")
        : el("button", { class: "btn btn-quiet btn-small", type: "button", onclick: () => openDetails(p) }, "Details")),
    ))),
  ));
}

document.getElementById("add-patient").addEventListener("click", () => openForm());

// ---- history ---------------------------------------------------------------------

const historyDays = document.getElementById("history-days");
historyDays.addEventListener("change", loadHistory);
document.getElementById("history-print").addEventListener("click", () => window.print());

const KIND_LABEL = { power: "Power outage", silent: "Unit silent", storm: "Storm notice", machine: "Machine stopped",
                     help: "Help button", temperature: "Room temperature" };
const KIND_TAG = { power: "tag-out", machine: "tag-out", help: "tag-out", silent: "", storm: "tag-storm", temperature: "tag-storm" };
const ADVISORY_KINDS = new Set(["silent", "storm", "temperature"]);

async function loadHistory() {
  const days = historyDays.value;
  document.getElementById("history-csv").href = `/api/history.csv?days=${days}`;
  const body = document.getElementById("history-body");
  const summary = document.getElementById("history-summary");
  let incidents;
  try {
    ({ incidents } = await guarded(api("GET", `/api/history?days=${days}`)));
  } catch (error) {
    summary.textContent = "";
    body.replaceChildren(el("p", { class: "form-error" }, error.message));
    return;
  }
  summary.textContent = historySummary(incidents, historyDays.selectedOptions[0].textContent.toLowerCase());
  document.getElementById("history-kpis").replaceChildren(...responseTiles(incidents));
  document.getElementById("history-charts").replaceChildren(...historyCharts(incidents, Number(days)));
  if (!incidents.length) {
    body.replaceChildren(el("div", { class: "empty" },
      el("h2", {}, "Nothing to report"),
      el("p", { class: "muted" }, "Power outages and silent units will be listed here as they happen.")));
    return;
  }
  body.replaceChildren(el("table", { class: "stack-table" },
    el("thead", {}, el("tr", {}, ["Started", "Patient", "What happened", "Lasted", "Alerted", "Response"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, incidents.map(historyRow))));
}

const EMERGENCY_KINDS = new Set(["power", "machine", "help"]);

function historySummary(incidents, period) {
  const outages = incidents.filter((i) => EMERGENCY_KINDS.has(i.kind));
  const silent = incidents.filter((i) => i.kind === "silent").length;
  const storms = incidents.filter((i) => i.kind === "storm").length;
  const silentNote = (silent ? ` ${plural(silent, "unit")} went silent.` : "") +
    (storms ? ` ${plural(storms, "storm notice")} sent ahead of bad weather.` : "");
  if (!outages.length) return `No emergencies in the ${period}.${silentNote}`;
  const answered = outages.filter((i) => i.response_minutes !== null).map((i) => i.response_minutes).sort((a, b) => a - b);
  const median = medianOf(answered);
  const response = answered.length
    ? `Someone responded to ${answered.length} of ${outages.length}, with a typical response time of ${durationText(median)}.`
    : "None acknowledged yet.";
  const count = `${outages.length} ${outages.length === 1 ? "emergency" : "emergencies"}`;
  return `${count} in the ${period}. ${response}${silentNote}`;
}

const ANSWER_TARGET_MINUTES = 15;

function medianOf(sorted) {
  return sorted.length ? sorted[Math.floor((sorted.length - 1) / 2)] : null;
}

// Daily buckets for a month or less, weekly beyond, oldest first.
function buckets(incidents, days) {
  const size = (days <= 30 ? 1 : 7) * 86400000;
  const count = Math.ceil((days * 86400000) / size);
  const start = Date.now() - count * size;
  const slots = Array.from({ length: count }, (_, i) => ({
    label: new Date(start + i * size).toLocaleDateString([], { month: "short", day: "numeric" }), incidents: [] }));
  for (const incident of incidents) {
    const index = Math.floor((new Date(incident.started_at).getTime() - start) / size);
    if (index >= 0 && index < count) slots[index].incidents.push(incident);
  }
  return { slots, per: days <= 30 ? "day" : "week" };
}

function historyCharts(incidents, days) {
  const emergencies = incidents.filter((i) => EMERGENCY_KINDS.has(i.kind));
  if (!emergencies.length) return [];
  const { slots, per } = buckets(emergencies, days);
  const counts = slots.map((s) => ({ label: s.label, value: s.incidents.length }));
  const responses = slots.map((s) => ({
    label: s.label,
    value: medianOf(s.incidents.filter((i) => i.response_minutes !== null).map((i) => i.response_minutes).sort((a, b) => a - b)),
  }));
  return [
    barChart(counts, { title: `Emergencies per ${per}`, note: "Power outages, stopped machines and help calls",
                       unit: (n) => (n === 1 ? "emergency" : "emergencies") }),
    lineChart(responses, { title: "Typical time to respond", note: `Median per ${per}, from the first alert`,
                           target: ANSWER_TARGET_MINUTES, targetLabel: `${ANSWER_TARGET_MINUTES} min goal`,
                           format: (v) => (v ? durationText(Math.round(v)) : "0") }),
  ];
}

// The numbers an agency is judged on: how fast someone responds, and how often nobody does.
function responseTiles(incidents) {
  const outages = incidents.filter((i) => EMERGENCY_KINDS.has(i.kind));
  if (!outages.length) return [];
  const answered = outages.filter((i) => i.response_minutes !== null).map((i) => i.response_minutes).sort((a, b) => a - b);
  const median = medianOf(answered);
  const onTime = answered.filter((m) => m <= ANSWER_TARGET_MINUTES).length;
  const missed = outages.filter((i) => i.response_minutes === null && i.ended_at !== null).length;
  const tile = (label, value, note, isAlert = false) => el("div", { class: isAlert ? "kpi is-alert" : "kpi" },
    el("p", { class: "kpi-label" }, label), el("p", { class: "kpi-value" }, value), note ? el("p", { class: "kpi-note" }, note) : null);
  return [
    tile("Emergencies", String(outages.length), "power outages, stopped machines and help calls"),
    tile("Typical time to respond", median === null ? "None yet" : durationText(median), "median, from the first alert"),
    tile(`Answered within ${ANSWER_TARGET_MINUTES} minutes`, `${onTime} of ${outages.length}`),
    tile("Ended with nobody responding", String(missed), missed ? "check these call lists" : null, missed > 0),
  ];
}

function historyRow(incident) {
  const ongoing = incident.ended_at === null;
  return el("tr", {},
    el("td", { class: "num", "data-label": "Started" }, dateTime(incident.started_at)),
    el("td", { "data-label": "Patient" }, el("strong", {}, incident.patient_name), el("div", { class: "muted code" }, incident.device_id)),
    el("td", { "data-label": "What happened" },
      el("span", { class: `tag ${KIND_TAG[incident.kind]}` }, KIND_LABEL[incident.kind]),
      el("div", { class: "muted" }, incident.detail || incident.equipment)),
    el("td", { class: "num", "data-label": "Lasted" },
      ongoing ? el("span", { class: "tag tag-ongoing" }, "Ongoing") : durationText(incident.duration_minutes)),
    el("td", { class: "cell-wide", "data-label": "Alerted" },
      incident.alerted.length ? listNames(incident.alerted.map((a) => a.name)) : "Nobody yet"),
    el("td", { class: "cell-wide", "data-label": "Response" }, responseText(incident)));
}

function responseText(incident) {
  if (ADVISORY_KINDS.has(incident.kind)) return el("span", { class: "muted" }, "Not needed");
  if (incident.acknowledged_by) return `${incident.acknowledged_by} after ${durationText(incident.response_minutes)}`;
  if (incident.ended_at === null && !incident.acknowledged_by) return "Waiting";
  return el("span", { class: "no-response" }, "No one responded");
}

// ---- unit health -------------------------------------------------------------------

function signalLevel(rssi) {
  if (rssi >= -60) return [4, "Strong"];
  if (rssi >= -70) return [3, "Good"];
  if (rssi >= -80) return [2, "Weak"];
  return [1, "Poor"];
}

function signalCell(rssi) {
  if (rssi === null) return el("span", { class: "muted" }, "Not reported");
  const [level, label] = signalLevel(rssi);
  return el("span", {},
    el("span", { class: "bars", "data-level": String(level), "aria-hidden": "true" }, el("span"), el("span"), el("span"), el("span")),
    `${label} (${rssi} dBm)`);
}

function sensorsText(unit) {
  const parts = [];
  if (unit.room_temp_f !== null) parts.push(`Room ${Math.round(unit.room_temp_f)}°F`);
  if (unit.load_amps !== null) parts.push(`machine drawing ${unit.load_amps.toFixed(1)} A`);
  if (unit.room_alert) parts.push(unit.room_alert);
  return parts.length ? parts.join(", ") : el("span", { class: "muted" }, "None fitted");
}

function renderUnits() {
  const body = document.getElementById("units-body");
  if (!lastUnits.length) {
    body.replaceChildren(el("div", { class: "empty" }, el("h2", {}, "No units yet"),
      el("p", { class: "muted" }, "Add a patient and set up their power unit to see it here.")));
    return;
  }
  const units = [...lastUnits].sort((a, b) => a.name.localeCompare(b.name));
  body.replaceChildren(el("table", { class: "stack-table" },
    el("thead", {}, el("tr", {}, ["Patient", "Status", "Last report", "Wi-Fi", "Sensors", "Firmware", "Running for"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, units.map((unit) => el("tr", {},
      el("td", { "data-label": "Patient" }, el("strong", {}, unit.name), el("div", { class: "muted code" }, unit.device_id)),
      el("td", { "data-label": "Status" }, el("span", { class: `tag ${stateOf(unit) === "out" ? "tag-out" : ""}` }, labelFor(unit))),
      el("td", { class: "num", "data-label": "Last report" }, unit.last_seen ? secondsAgo(unit.last_seen) : "Never"),
      el("td", { class: "cell-wide", "data-label": "Wi-Fi" }, signalCell(unit.rssi)),
      el("td", { "data-label": "Sensors" }, sensorsText(unit)),
      el("td", { class: "code", "data-label": "Firmware" }, unit.firmware ?? "Unknown"),
      el("td", { class: "num", "data-label": "Running for" }, unit.uptime_s === null ? "Unknown" : uptimeText(unit.uptime_s)),
    )))));
}

// ---- add / edit panel ----------------------------------------------------------

const panel = document.getElementById("panel");
const panelContent = document.getElementById("panel-content");
panel.querySelector("[data-close]").addEventListener("click", () => panel.close());

function field(label, control, hint, className = "field") {
  control.id ||= `f-${control.name}`;
  return el("div", { class: className }, el("label", { for: control.id }, label), control, hint ? el("p", { class: "hint" }, hint) : null);
}

function textarea(name, value = "", placeholder = "") {
  return el("textarea", { class: "input textarea", name, rows: 3, placeholder, value: value ?? "" });
}

function input(name, value = "", props = {}) {
  return el("input", { class: "input", name, value: value ?? "", ...props });
}

function select(name, options, value) {
  return el("select", { class: "input", name },
    options.map(([optionValue, text]) => el("option", { value: optionValue, selected: optionValue === value }, text)));
}

function contactRow(contact = {}) {
  // Rows repeat, so every control gets a unique id for its label.
  const unique = (control) => { control.id = `c-${crypto.randomUUID()}`; return control; };
  const row = el("div", { class: "contact-row" },
    el("span", { class: "contact-order", "aria-hidden": "true" }),
    field("Name", unique(input("c-name", contact.name, { required: true, autocomplete: "off" }))),
    field("Role", unique(select("c-role", ROLES.map((r) => [r, r]), contact.role || "Family"))),
    field("Phone", unique(input("c-phone", contact.phone, { type: "tel", placeholder: "+1 601 555 0123", required: true })), null, "field field-wide"),
    field("Reach by", unique(select("c-channel", [["sms", "Text"], ["call", "Phone call"]], contact.channel || "sms"))),
    el("button", { class: "btn btn-quiet btn-small", type: "button", "aria-label": "Remove contact", onclick: () => { row.remove(); renumber(); } }, "Remove"),
  );
  return row;
}

function renumber() {
  panelContent.querySelectorAll(".contact-order").forEach((badge, index) => { badge.textContent = `${index + 1}`; });
}

function openForm(patient) {
  const editing = Boolean(patient);
  document.getElementById("panel-title").textContent = editing ? `Edit ${patient.name}` : "Add patient";
  const contacts = el("div", { id: "contacts", class: "contacts" }, (patient?.contacts?.length ? patient.contacts : [{}]).map(contactRow));
  const error = el("p", { class: "form-error", role: "alert", hidden: true });
  const save = el("button", { class: "btn", type: "submit" }, editing ? "Save changes" : "Add patient");

  const form = el("form", { novalidate: true },
    error,
    field("Patient name", input("name", patient?.name, { required: true, autocomplete: "off" })),
    field("Home address", input("address", patient?.address, { required: true, autocomplete: "off" })),
    locateRow(),
    el("div", { class: "grid-2" },
      field("Latitude", input("lat", patient?.lat, { inputmode: "decimal", required: true })),
      field("Longitude", input("lon", patient?.lon, { inputmode: "decimal", required: true }))),
    el("div", { class: "grid-2" },
      field("Equipment", input("equipment", patient?.equipment, { required: true, placeholder: "Oxygen concentrator" })),
      field("Backup minutes", input("backup_minutes", patient?.backup_minutes, { type: "number", min: 0, max: 2880, required: true }),
        "How long the equipment runs on its own battery.")),
    el("div", { class: "grid-2" },
      field("Acuity", select("acuity", [["1", "1, most critical"], ["2", "2"], ["3", "3, least critical"]], String(patient?.acuity ?? 1))),
      editing ? field("Unit ID", input("device_id", patient.device_id, { disabled: true }), "Fixed once the unit is set up.")
              : field("Unit ID", input("device_id", "", { required: true, placeholder: "unit-07", pattern: "[a-z0-9-]{3,40}" }),
                  "Printed on the power unit.")),
    el("fieldset", {},
      el("legend", {}, "Emergency plan"),
      el("p", { class: "hint muted" }, "Shown to the family when the power goes out or a storm is coming. All optional."),
      field("Backup supplies", textarea("plan-supplies", patient?.plan?.supplies, "2 oxygen tanks in the hall closet; spare battery charged")),
      el("div", { class: "grid-2" },
        field("Nearest place with power", input("plan-place_name", patient?.plan?.place_name, { placeholder: "Forrest General Hospital" })),
        field("Its phone", input("plan-place_phone", patient?.plan?.place_phone, { type: "tel" }))),
      field("Its address", input("plan-place_address", patient?.plan?.place_address)),
      field("What to do", textarea("plan-steps", patient?.plan?.steps, "1. Switch to the oxygen tank.\n2. Call the agency on-call line.\n3. If breathing is hard, call 911."))),
    el("fieldset", {},
      el("legend", {}, "Machine sensor"),
      field("Running current, in amps", input("running_amps_min", patient?.running_amps_min, { inputmode: "decimal", placeholder: "Leave blank if no clamp is fitted" }),
        "With a current clamp on the equipment's cord: below this, while the power is on, we alert that the machine has stopped.")),
    el("fieldset", {},
      el("legend", {}, "Call list"),
      el("p", { class: "hint muted" }, "We alert the first person right away, then the next one every few minutes until someone responds."),
      contacts,
      el("button", { class: "btn btn-quiet btn-small", type: "button", onclick: () => { contacts.append(contactRow()); renumber(); } }, "Add contact")),
    el("div", { class: "panel-actions" },
      save,
      el("button", { class: "btn btn-quiet", type: "button", onclick: () => panel.close() }, "Cancel"),
      editing ? removeButton(patient) : null),
  );

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    try {
      const body = readForm(form);
      const saved = await withButton(save, () => guarded(editing
        ? api("PUT", `/api/patients/${encodeURIComponent(patient.device_id)}`, body)
        : api("POST", "/api/patients", body)));
      await loadRoster();
      refreshBoard();
      showCredentials(saved, editing ? "Changes saved" : "Patient added");
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      error.scrollIntoView({ block: "nearest" });
    }
  });

  panelContent.replaceChildren(form);
  if (editing) panelContent.append(credentialsBlock(patient), recentIncidentsBlock(patient));
  renumber();
  panel.showModal();
  form.querySelector("input:not([disabled])").focus();
}

// Read only, for care staff: how to reach the patient and their people, and what the plan says.
function openDetails(patient) {
  document.getElementById("panel-title").textContent = patient.name;
  const plan = patient.plan ?? {};
  const fact = (label, value) => (value || value === 0) ? el("div", {}, el("dt", {}, label), el("dd", {}, String(value))) : null;
  panelContent.replaceChildren(
    el("div", { class: "panel-body" },
      el("dl", { class: "facts" },
        fact("Address", patient.address),
        fact("Equipment", patient.equipment),
        fact("Backup", durationText(patient.backup_minutes)),
        fact("Acuity", patient.acuity),
        fact("Unit ID", patient.device_id))),
    el("div", { class: "panel-body" },
      el("h3", {}, "Call list, in order"),
      el("ol", { class: "call-list" }, patient.contacts.map((c) => el("li", {},
        el("strong", {}, c.name), ` (${c.role}), `, el("a", { href: `tel:${c.phone}` }, c.phone), c.channel === "call" ? ", by call" : ", by text")))),
    el("div", { class: "panel-body" },
      el("h3", {}, "Emergency plan"),
      Object.values(plan).some(Boolean)
        ? el("dl", { class: "facts" },
            fact("Backup supplies", plan.supplies),
            fact("Nearest place with power", [plan.place_name, plan.place_address, plan.place_phone].filter(Boolean).join(", ")),
            fact("What to do", plan.steps))
        : el("p", { class: "muted" }, "No plan yet. Ask an admin to add one.")),
    recentIncidentsBlock(patient),
    el("div", { class: "panel-body" }, el("div", { class: "panel-actions" },
      el("button", { class: "btn", type: "button", onclick: () => panel.close() }, "Close"))),
  );
  panel.showModal();
}

// ---- staff and roles (admins) ------------------------------------------------------

const staffForm = document.getElementById("staff-add");
const staffError = document.getElementById("staff-error");
let roleOptions = [];

async function loadStaff() {
  const body = document.getElementById("staff-body");
  let staff;
  try {
    ({ staff, roles: roleOptions } = await guarded(api("GET", "/api/staff")));
  } catch (error) {
    body.replaceChildren(el("p", { class: "form-error" }, error.message));
    return;
  }
  const roleSelect = document.getElementById("staff-role");
  if (!roleSelect.options.length) {
    roleSelect.replaceChildren(...roleOptions.map((r) => el("option", { value: r.value }, r.label)));
    roleSelect.value = "nurse";  // the most common grant; admin is a deliberate choice
  }
  body.replaceChildren(el("table", { class: "stack-table" },
    el("thead", {}, el("tr", {}, ["Email", "Role", ""].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, staff.map(staffRow))));
}

function staffRow(person) {
  const isMe = person.email === me.email;
  const role = el("select", { class: "input input-small", "aria-label": `Role for ${person.email}`, disabled: isMe },
    roleOptions.map((r) => el("option", { value: r.value, selected: r.value === person.role }, r.label)));
  const note = el("span", { class: "form-error", role: "alert", hidden: true });
  role.addEventListener("change", async () => {
    note.hidden = true;
    try {
      await guarded(api("PUT", "/api/staff", { email: person.email, role: role.value }));
    } catch (error) {
      note.textContent = error.message;
      note.hidden = false;
      role.value = person.role;
    }
  });
  const remove = el("button", { class: "btn btn-danger btn-small", type: "button", hidden: isMe }, "Remove access");
  remove.addEventListener("click", async () => {
    if (remove.dataset.confirm !== "yes") {
      remove.dataset.confirm = "yes";
      remove.textContent = "Confirm remove";
      return;
    }
    try {
      await withButton(remove, () => guarded(api("POST", "/api/staff/remove", { email: person.email })));
      loadStaff();
    } catch (error) {
      note.textContent = error.message;
      note.hidden = false;
    }
  });
  return el("tr", {},
    el("td", { "data-label": "Email" }, el("strong", {}, person.email), isMe ? el("span", { class: "muted" }, " (you)") : null),
    el("td", { "data-label": "Role" }, role, note),
    el("td", {}, remove));
}

staffForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  staffError.hidden = true;
  try {
    const added = await withButton(staffForm.querySelector("button"), () =>
      guarded(api("POST", "/api/staff", { email: staffForm.email.value.trim(), role: staffForm.role.value })));
    staffForm.email.value = "";
    loadStaff();
    showNewAccount(added);
  } catch (error) {
    staffError.textContent = error.message;
    staffError.hidden = false;
  }
});

// The temporary password exists only in this response; once the panel closes, nobody can see it again.
function showNewAccount(added) {
  const label = roleOptions.find((r) => r.value === added.role)?.label ?? added.role;
  document.getElementById("panel-title").textContent = `${added.email} can now sign in`;
  const steps = added.temporary_password
    ? [copyRow("Temporary password", added.temporary_password, true),
       el("p", {}, `Give them this password in person or by phone, not in the same message as the link. They sign in at ${location.origin}/login as ${label}, then choose their own password under "Change password".`),
       el("p", { class: "hint muted" }, "This password is shown only once. If it is lost, remove their access and add them again for a new one.")]
    : [el("p", {}, `They already had a sign-in account, so they keep their current password. Their role is now ${label}.`)];
  panelContent.replaceChildren(el("div", { class: "panel-body" }, ...steps,
    el("div", { class: "panel-actions" }, el("button", { class: "btn", type: "button", onclick: () => panel.close() }, "Done"))));
  panel.showModal();
}

// Fills latitude and longitude from the address, so staff don't look them up by hand.
function locateRow() {
  const status = el("p", { class: "hint muted", "aria-live": "polite" }, "Or right-click the home in Google Maps to copy its latitude and longitude.");
  const button = el("button", { class: "btn btn-quiet btn-small", type: "button" }, "Find location from address");
  button.addEventListener("click", async () => {
    const form = button.closest("form");
    try {
      const { matches } = await withButton(button, () => guarded(api("POST", "/api/geocode", { address: form.elements.address.value })));
      if (!matches.length) {
        status.textContent = "No match. Check the spelling, add the town and state, or enter the location by hand.";
        return;
      }
      form.elements.lat.value = matches[0].lat.toFixed(5);
      form.elements.lon.value = matches[0].lon.toFixed(5);
      status.textContent = `Found ${matches[0].address}. Check it's the right home.`;
    } catch (error) {
      status.textContent = error.message;
    }
  });
  return el("div", { class: "locate-row" }, button, status);
}

function readForm(form) {
  const value = (name) => form.elements[name].value.trim();
  const contacts = [...form.querySelectorAll(".contact-row")].map((row) => ({
    name: row.querySelector("[name=c-name]").value.trim(),
    role: row.querySelector("[name=c-role]").value,
    phone: row.querySelector("[name=c-phone]").value.trim(),
    channel: row.querySelector("[name=c-channel]").value,
  }));
  if (!contacts.length) throw new Error("Add at least one person to the call list.");
  const missing = ["name", "address", "lat", "lon", "equipment", "backup_minutes"].filter((n) => !value(n));
  if (missing.length) throw new Error("Fill in every field before saving.");
  return {
    device_id: value("device_id"), name: value("name"), address: value("address"),
    lat: Number(value("lat")), lon: Number(value("lon")), equipment: value("equipment"),
    backup_minutes: Number(value("backup_minutes")), acuity: Number(value("acuity")), contacts,
    plan: Object.fromEntries(["supplies", "place_name", "place_address", "place_phone", "steps"].map((part) => [part, value(`plan-${part}`)])),
    running_amps_min: value("running_amps_min") === "" ? null : Number(value("running_amps_min")),
  };
}

function removeButton(patient) {
  const button = el("button", { class: "btn btn-danger push", type: "button" }, "Remove patient");
  button.addEventListener("click", async () => {
    if (button.dataset.confirm !== "yes") {
      button.dataset.confirm = "yes";
      button.textContent = `Confirm: stop monitoring ${patient.name}`;
      return;
    }
    await withButton(button, () => guarded(api("DELETE", `/api/patients/${encodeURIComponent(patient.device_id)}`)));
    panel.close();
    loadRoster();
    refreshBoard();
  });
  return button;
}

function recentIncidentsBlock(patient) {
  const list = el("ul", { class: "timeline" }, el("li", { class: "muted" }, "Loading…"));
  guarded(api("GET", `/api/history?days=90&device_id=${encodeURIComponent(patient.device_id)}`))
    .then(({ incidents }) => {
      list.replaceChildren(...(incidents.length
        ? incidents.slice(0, 10).map((i) => el("li", {},
            el("strong", {}, `${i.kind === "storm" ? i.detail : KIND_LABEL[i.kind]}, ${dateTime(i.started_at)}`),
            el("span", {}, [i.ended_at === null ? "Still going." : `Lasted ${durationText(i.duration_minutes)}.`,
                            i.kind === "storm" ? " Storm notice sent to the first contact." : i.kind === "power" ? (i.acknowledged_by ? ` ${i.acknowledged_by} responded after ${durationText(i.response_minutes)}.` : " Nobody acknowledged it.") : ""].join(""))))
        : [el("li", { class: "muted" }, "No outages in the last 90 days.")]));
    })
    .catch((error) => list.replaceChildren(el("li", { class: "form-error" }, error.message)));
  return el("div", { class: "panel-body" }, el("h3", {}, "Recent incidents"), list);
}

// ---- credentials: what the installer and the family need -------------------------

function copyRow(label, value, isSecret) {
  const button = el("button", { class: "btn btn-quiet btn-small", type: "button" }, "Copy");
  button.addEventListener("click", () => withButton(button, () => navigator.clipboard.writeText(value), { successLabel: "Copied" }));
  return el("div", { class: "credential" },
    el("div", { class: "credential-row" }, el("strong", {}, label), button),
    el("span", { class: isSecret ? "code" : "" }, value));
}

function credentialsBlock(patient) {
  const rotate = el("button", { class: "btn btn-quiet btn-small", type: "button" }, "Make a new family link");
  rotate.addEventListener("click", async () => {
    const updated = await withButton(rotate, () => guarded(api("POST", `/api/patients/${encodeURIComponent(patient.device_id)}/rotate-link`, {})));
    showCredentials(updated, "New family link made. The old one no longer works.");
  });
  return el("div", { class: "panel-body" },
    el("h3", {}, "Set-up details"),
    copyRow("Unit ID", patient.device_id, true),
    copyRow("Unit token (goes in the unit's config.h)", patient.device_token, true),
    copyRow("Family status link", patient.status_link, false),
    el("p", { class: "hint muted" }, "Anyone with the family link can see this patient's power status. If it was shared with the wrong person, make a new one."),
    rotate);
}

function showCredentials(patient, title) {
  document.getElementById("panel-title").textContent = title;
  panelContent.replaceChildren(
    credentialsBlock(patient),
    el("div", { class: "panel-body" }, el("div", { class: "panel-actions" },
      el("button", { class: "btn", type: "button", onclick: () => panel.close() }, "Done"),
      el("button", { class: "btn btn-quiet", type: "button", onclick: () => openForm(patient) }, "Edit details"))),
  );
}

// ---- start -----------------------------------------------------------------------

startClock(document.getElementById("view-board"));

showView();
refreshBoard();
setInterval(refreshBoard, BOARD_REFRESH_MS);
refreshWeather();
setInterval(refreshWeather, WEATHER_REFRESH_MS);
