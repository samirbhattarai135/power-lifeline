import { createBoardMap } from "./map.js";
import { barChart, lineChart } from "./charts.js";
import { TRIAGE_AT, TRIAGE_FIELDS, actionCard, countGrid, countsOf, icon, needsAction, renderKeyed, situationChips, startClock, townOf, triageOrder, triageRow, triageTools } from "./actions.js";
import { applyFilters, filterBar, isFiltering, mountFilterBar, resetState } from "./filters.js";
import { createAlarm } from "./alarm.js";
import { ApiError, api, el, chip, clockTime, dateTime, durationText, glyph, hazardText, homeState, STATE_WORD, listNames, minutesSince, nameNode, nameParts, plural, secondsAgo, stateOf, uptimeText, withButton } from "./common.js";

const BOARD_REFRESH_MS = 2000;
const MAP_CENTER = [31.3, -89.31]; // Pine Belt, Mississippi
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
  await api("POST", "/api/logout", {}).catch(() => {});  // offline: still leave the board
  location.assign("/login");
});
const accountToggle = document.getElementById("account-toggle");
accountToggle.addEventListener("click", () => {
  const open = accountToggle.getAttribute("aria-expanded") !== "true";
  accountToggle.setAttribute("aria-expanded", String(open));
  document.getElementById("account-menu").classList.toggle("is-open", open);
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
  const state = homeState(unit);
  if (state === "out" && unit.emergency && unit.emergency !== "power") return EMERGENCY_LABEL[unit.emergency];
  return unit.status === "never_seen" ? "Not set up" : STATE_WORD[state];
}

// ---- the list beside the map ------------------------------------------------------------
// Up to TRIAGE_AT homes needing someone: full action cards. From then on (a storm), a ranked
// one-line-per-home list with numbered map pins; a tapped line opens its full card.

const openRows = new Set();
let wasTriage = false;
const filters = { town: null, status: null, equipment: null, search: "" };  // e.g. one town's homes, to send one person to a cluster
let sendNote = "";
const locate = (unit) => boardMap.focus(unit);

function renderBoardList(units) {
  const active = triageOrder(units);
  const triage = active.length >= TRIAGE_AT;
  const container = document.getElementById("units");
  container.classList.toggle("is-triage", triage);
  const ranks = new Map(active.map((u, index) => [u.device_id, index + 1]));
  const filtering = isFiltering(TRIAGE_FIELDS, filters);
  const shown = applyFilters(active, TRIAGE_FIELDS, filters);
  const ordered = triage ? [...shown, ...(filtering ? [] : units.filter((u) => !needsAction(u)))] : boardFilters.mount(units);
  const tools = document.getElementById("triage-tools");
  tools.hidden = !triage;
  document.getElementById("board-tools").hidden = triage;
  // Rebuilt only when what it shows changes: a heartbeat must not swallow a tap or reset the two-tap send.
  const toolsKey = JSON.stringify([active.map((u) => [u.address, u.equipment, u.status, u.critical, u.unreached, u.acknowledged_by]), filters]);
  if (triage && tools.dataset.key !== toolsKey) {
    tools.dataset.key = toolsKey;
    mountFilterBar(tools, triageTools(active, { state: filters, onChange: setFilter, onReset: resetFilters }));
  }
  renderKeyed(container,
    ordered.map((u) => ({ ...u, open: openRows.has(u.device_id), ...(triage && ranks.has(u.device_id) ? { rank: ranks.get(u.device_id) } : {}) })),
    (u) => (u.rank ? triageRow(u, u.rank, { respondButton, canRespond: can("respond"), open: u.open, onToggle: toggleRow, onLocate: locate })
      : needsAction(u) ? actionCard(u, { respondButton, canRespond: can("respond"), onLocate: locate }) : unitRow(u)));

  if (triage && !wasTriage) boardMap.fitTo(active);
  wasTriage = triage;
}

function setFilter(key, value) {
  filters[key] = value;
  renderBoardList(lastUnits);
  fitShown();
}

function resetFilters() {
  resetState(TRIAGE_FIELDS, filters);
  renderBoardList(lastUnits);
  fitShown();
}

// The active homes the filters leave on the list.
const shownActive = () => applyFilters(triageOrder(lastUnits), TRIAGE_FIELDS, filters);

// Back to the big picture: every shown active home.
function fitShown() {
  const shown = shownActive();
  const active = triageOrder(lastUnits);
  boardMap.fitTo(shown.length ? shown : active.length ? active : lastUnits);
}

// Two taps, because it sends messages: the first arms it, the second sends. The arming lives here,
// not on the button, because a storm rebuilds the button every refresh as the count changes.
const COUNTY_ARM_MS = 8000;
let countyArmedUntil = 0;

function sendCountyButton(count) {
  const armed = () => Date.now() < countyArmedUntil;
  const button = el("button", { class: "btn btn-county", type: "button" },
    armed() ? "Tap again to send the welfare-check list" : `Send ${count} no-answer home${count === 1 ? "" : "s"} to county`);
  if (armed()) button.dataset.confirm = "yes";
  button.addEventListener("click", async () => {
    if (!armed()) {
      countyArmedUntil = Date.now() + COUNTY_ARM_MS;
      button.dataset.confirm = "yes";
      button.textContent = "Tap again to send the welfare-check list";
      setTimeout(() => { if (!armed()) { headlineKey = ""; headline(lastUnits); } }, COUNTY_ARM_MS + 50);  // disarm on screen too
      return;
    }
    countyArmedUntil = 0;
    try {
      const result = await withButton(button, () => guarded(api("POST", "/api/county/notify", {})));
      sendNote = (result.sent_to.length ? `Welfare-check list sent to ${listNames(result.sent_to)} at ${clockTime(new Date().toISOString())}.` : "")
        + (result.no_county_contact.length ? ` No county contact for ${listNames(result.no_county_contact)}: call the county directly.` : "");
    } catch (error) {
      sendNote = error.message;
    }
    headline(lastUnits);
  });
  return button;
}

function toggleRow(unit) {
  if (openRows.has(unit.device_id)) openRows.delete(unit.device_id);
  else openRows.add(unit.device_id);
  renderBoardList(lastUnits);
  // The map follows the most recently opened home still on the list, else shows them all.
  const listed = new Set([...document.querySelectorAll("#units > [data-key]")].map((node) => node.dataset.key));
  const openUnits = [...openRows].filter((id) => listed.has(id)).map((id) => lastUnits.find((u) => u.device_id === id)).filter(Boolean);
  if (openUnits.length) locate(openUnits.at(-1));
  else fitShown();
}

// A pin on the map was tapped: open that home's line and bring it into view.
function selectDevice(deviceId) {
  const unit = lastUnits.find((u) => u.device_id === deviceId);
  if (unit) jumpTo(unit);
}

// A chip, pin or name was tapped: bring that home's card into view and show it on the map.
function jumpTo(unit) {
  if (wasTriage && !openRows.has(unit.device_id)) { openRows.add(unit.device_id); renderBoardList(lastUnits); }
  const card = document.querySelector(`#units [data-key="${CSS.escape(unit.device_id)}"]`);
  card?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  card?.querySelector("button, a")?.focus({ preventScroll: true });
  boardMap.focus(unit);
}

// Freshness: the band says when it last heard from the server, and turns grey when it can't.
const liveNode = el("p", { class: "live" }, el("span", { class: "live-dot", "aria-hidden": "true" }), el("span", { class: "live-text" }, "Connecting"));
let lastGood = null;

function markLive(ok) {
  if (ok) lastGood = new Date();
  const since = lastGood ? clockTime(lastGood.toISOString()) : null;
  const text = ok ? `Live · ${since}` : since ? `Not updating since ${since}` : "Not updating";
  const label = liveNode.querySelector(".live-text");
  if (label.textContent !== text) label.textContent = text;
  liveNode.classList.toggle("is-stale", !ok);
  for (const badge of document.querySelectorAll(".page-live")) {
    badge.classList.toggle("is-stale", !ok);
    const pageLabel = badge.querySelector(".live-text");
    if (pageLabel.textContent !== text) pageLabel.textContent = text;
  }
  document.getElementById("headline").classList.toggle("is-stale", !ok);
  if (!ok) announce(`${text}. The board may be out of date.`);
}

let headlineKey = "";

// Rebuilt only when what it shows changes, so a refresh never swallows a tap on the county button.
function headline(units) {
  const out = units.filter((u) => u.status === "outage");
  const waiting = out.filter((u) => !u.acknowledged_by);
  const node = document.getElementById("headline");
  node.classList.toggle("is-alarm", waiting.length > 0);
  node.classList.toggle("is-handled", out.length > 0 && !waiting.length);
  document.body.classList.toggle("has-outage", out.length > 0);
  // Arming is not in the key: the armed button already shows it, and a rebuild between two taps could eat the second.
  const key = JSON.stringify([units.length, out.map((u) => u.emergency ?? "power"), countsOf(units).map((c) => c.matches.map((u) => u.device_id)), sendNote]);
  if (key === headlineKey) return;
  headlineKey = key;
  const title = (text) => el("div", { class: "headline-title" }, el("h1", {}, text), liveNode);
  if (!units.length) {
    node.replaceChildren(title("No patients yet"), el("p", { class: "headline-sub" }, "Add a patient to start monitoring their power."));
  } else if (!out.length) {
    node.replaceChildren(...[title(`All ${plural(units.length, "home")} have power`),
      el("p", { class: "headline-sub" }, "We'll alert the call list the moment that changes."), situationChips(units, jumpTo)].filter(Boolean));
  } else {
    const allPower = out.every((u) => (u.emergency ?? "power") === "power");
    node.replaceChildren(...[title(allPower ? `${plural(out.length, "home")} without power` : `${plural(out.length, "home")} need help now`),
      countGrid(units, jumpTo), countySend(units)].filter(Boolean));
  }
}

function countySend(units) {
  const unreached = units.filter((u) => u.status === "outage" && u.unreached && !u.acknowledged_by).length;
  if (!can("respond") || (!unreached && !sendNote)) return null;
  return el("div", { class: "county-send" },
    unreached ? sendCountyButton(unreached) : null,
    unreached ? el("p", { class: "county-hint" }, "Tap again to confirm · read-only list, no phone numbers") : null,
    sendNote ? el("p", { class: "send-note", role: "status" }, sendNote) : null);
}

/** What screen readers hear when the counts change: the title and each non-zero count. */
function countsSentence(units) {
  const counts = countsOf(units).filter((c) => c.matches.length).map((c) => `${c.matches.length} ${c.label}`);
  return [document.querySelector("#headline h1")?.textContent, counts.join(", ")].filter(Boolean).join(". ");
}

// A row newly crossing under 15 minutes gets one flash and one assertive announcement, then holds still.
let knownCritical = null;
function flagNewlyRed(units) {
  const critical = units.filter((u) => homeState(u) === "critical");
  if (knownCritical) {
    const fresh = critical.filter((u) => !knownCritical.has(u.device_id));
    for (const unit of fresh) document.querySelector(`#units [data-key="${CSS.escape(unit.device_id)}"]`)?.classList.add("is-new-red");
    if (fresh.length) {
      document.getElementById("announce-alert").textContent =
        `Under 15 minutes of backup: ${listNames(fresh.map((u) => nameParts(u.name)[0])).replace(/\.$/, "")}. Call 911 if nobody is there.`;
    }
  }
  knownCritical = new Set(critical.map((u) => u.device_id));
}

// One line per hazard, naming a few of the patients in its path; high risk first (the server sorts each unit's list).
function stormBanner(units) {
  const byHazard = new Map();
  for (const unit of units) {
    for (const hazard of unit.hazards) {
      const key = `${hazard.level}|${hazardText(hazard)}`;
      if (!byHazard.has(key)) byHazard.set(key, { hazard, names: [] });
      byHazard.get(key).names.push(nameParts(unit.name)[0]);
    }
  }
  const node = document.getElementById("storm-banner");
  node.hidden = byHazard.size === 0;
  if (!byHazard.size) return;
  const lines = [...byHazard.values()].sort((a, b) => (a.hazard.level === "high" ? -1 : 1) - (b.hazard.level === "high" ? -1 : 1));
  node.replaceChildren(
    el("h2", {}, "Storm watch"),
    ...lines.map(({ hazard, names }) => el("p", { class: `storm-line is-${hazard.level}` }, el("strong", {}, hazardText(hazard)),
      `: ${plural(names.length, "home")} in the path`, names.length > 3 ? `, including ${names.slice(0, 3).join(", ")}…` : `: ${listNames(names)}.`)),
    callFirst(units) ?? "",
    el("p", { class: "storm-sent" }, "High-risk warnings have been sent to each patient's first contact. Check backups now, most critical first."),
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
  return el("div", { class: "call-first" },
    el("p", { class: "call-first-head" }, el("strong", {}, "Call first"), el("span", { class: "muted" }, "most critical first, then shortest backup")),
    el("ol", {}, urgent.map((u, index) => el("li", {},
      el("span", { class: "call-first-index" }, String(index + 1)),
      el("div", { class: "call-first-who" }, el("p", {}, nameNode(u.name)), el("p", { class: "muted" }, `${durationText(u.backup_minutes)} of backup`)),
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

// A home that needs nobody right now: one quiet line, the state word on the right.
// A quiet home: tapping it shows it on the map and, for staff who may see call lists, opens its
// details right under the row (a side panel would cover the map it just moved).
function unitRow(unit) {
  const state = homeState(unit);
  const report = unit.status === "never_seen" ? "unit has not connected yet"
    : `last report ${secondsAgo(unit.last_seen)}${unit.battery_percent == null ? "" : `, unit battery ${unit.battery_percent}%`}`;
  const hasDetails = can("view_patients") && Boolean(unit.contacts);
  const select = el("button", { class: "home-select", type: "button", "aria-expanded": hasDetails ? String(Boolean(unit.open)) : null,
    onclick: () => (hasDetails ? toggleRow(unit) : locate(unit)) },
    el("span", { class: "home-main" },
      el("span", { class: "home-name" }, nameNode(unit.name), unit.hazards.length ? el("span", { class: "tag tag-storm" }, "In storm path") : null),
      el("span", { class: "home-meta" }, `${townOf(unit)} · ${unit.equipment} · ${report}`)),
    el("span", { class: "home-state" }, glyph(state), unit.status === "never_seen" ? "Not set up" : "Power on"));
  return el("article", { class: `unit home-row is-${state}${unit.open ? " is-open" : ""}` },
    el("h2", { class: "home-heading" }, select),
    unit.open && hasDetails ? el("div", { class: "home-details" }, ...patientDetails(unit)) : null);
}

function respondButton(unit) {
  if (!can("respond")) return el("p", { class: "muted" }, "Waiting for someone on the call list to respond.");
  const button = el("button", { class: "btn btn-respond", type: "button" }, icon("hand"), "I'm responding");
  button.addEventListener("click", () =>
    withButton(button, () => guarded(api("POST", `/api/ack/${encodeURIComponent(unit.device_id)}`, {}))).then(refreshBoard));
  return button;
}

const alarm = createAlarm(document.getElementById("sound-toggle"));
const boardMap = createBoardMap(document.getElementById("map"), { center: MAP_CENTER, labelFor, onSelect: (id) => selectDevice(id) });
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
    markLive(true);
    const snapshot = JSON.stringify(units);
    // Rebuilding unchanged rows every poll would swallow clicks on "We're responding".
    if (snapshot === lastBoard) return;
    lastBoard = snapshot;
    lastUnits = units;
    alarm.notice(units);
    if (currentView() === "units") renderUnits();
    if (currentView() === "patients" && roster.length && roster.map((p) => displayState(liveUnit(p.device_id))).join() !== rosterStatusKey) renderRoster();
    headline(units);
    stormBanner(units);
    attentionPanel(units);
    renderBoardList(units);
    flagNewlyRed(units);
    announce(countsSentence(units));
    boardMap.showUnits(units);
  } catch (error) {
    lastBoard = "";  // force a full redraw once the server answers again
    // Access removed or role changed mid-shift: /login sends someone with a role to their own page,
    // and someone without one to the sign-in form (not the public landing page).
    if (error instanceof ApiError && error.status === 403) location.assign("/login");
    else if (!(error instanceof ApiError && error.status === 401)) markLive(false);
  }
}

// ---- list pages: one structure everywhere (filter bar, then the rows that need action first) ----

const townFromAddress = (address = "") => address.split(",").slice(-2, -1)[0]?.trim() || "Unknown town";
const liveUnit = (deviceId) => lastUnits.find((u) => u.device_id === deviceId);
const LIVE_STATUS = { out: "Power out", acked: "Someone responding", quiet: "No signal", on: "Power on" };
const STATE_ORDER = ["out", "acked", "quiet", "on"];

// The list pages speak the board's words: one shape and one word per state, plus "Not set up".
const DISPLAY_WORD = { ...STATE_WORD, unset: "Not set up" };
const displayState = (unit) => (!unit || unit.status === "never_seen" ? "unset" : homeState(unit));
const URGENT_FIRST = ["critical", "unreached", "out", "responding", "silent", "unset", "on"];
const SILENT_FIRST = ["silent", "unset", "critical", "unreached", "out", "responding", "on"];  // unit health: dead units lead
const rankIn = (order) => (state) => order.indexOf(state);

function statusCell(unit) {
  const state = displayState(unit);
  return chip(state === "unset" ? "silent" : state, DISPLAY_WORD[state]);
}

function acuityBadge(acuity) {
  return el("span", { class: `acuity acuity-${acuity}`, title: acuity === 1 ? "Most critical" : acuity === 3 ? "Least critical" : null }, String(acuity));
}

/** Wire a page's filter bar: every change re-renders that page. */
function listFilters(fields, state, render, { idPrefix, noun, searchText }) {
  let shownKey = "";
  return {
    mount(items) {
      // Remount only when the options or choices change: a heartbeat must not close an open dropdown.
      const key = JSON.stringify([items.map((item) => fields.map((f) => f.value(item))), state]);
      if (key === shownKey) return applyFilters(items, fields, state, searchText);
      shownKey = key;
      mountFilterBar(document.getElementById(`${idPrefix}-tools`), filterBar(items, fields, state, {
        onChange: (key, value) => { state[key] = value; render(); },
        onReset: () => { resetState(fields, state); render(); },
        onSearch: searchText ? (text) => { state.search = text; render(); } : undefined,
        searchLabel: searchText ? "Search" : undefined, searchText, idPrefix, noun,
      }));
      return applyFilters(items, fields, state, searchText);
    },
  };
}

// A quiet night: every home, filterable by the same questions the rest of Lifeline asks.
const BOARD_FIELDS = [
  { key: "status", label: "Status", value: stateOf, names: LIVE_STATUS, order: STATE_ORDER },
  { key: "town", label: "Town", value: townOf },
  { key: "equipment", label: "Equipment", value: (u) => u.equipment },
];
const boardState = { status: null, town: null, equipment: null, search: "" };
const boardFilters = listFilters(BOARD_FIELDS, boardState, () => renderBoardList(lastUnits), {
  idPrefix: "board", noun: "homes", searchText: (u) => `${u.name} ${u.address} ${u.device_id}` });

const noMatch = (noun) => el("div", { class: "empty" }, el("h2", {}, `No ${noun} match these filters`),
  el("p", { class: "muted" }, "Remove a filter above, or reset them."));

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
  renderRoster();
}

const ROSTER_FIELDS = [
  { key: "status", label: "Status", value: (p) => displayState(liveUnit(p.device_id)), names: DISPLAY_WORD, order: URGENT_FIRST },
  { key: "town", label: "Town", value: (p) => townFromAddress(p.address) },
  { key: "equipment", label: "Equipment", value: (p) => p.equipment },
  { key: "acuity", label: "Acuity", value: (p) => String(p.acuity), names: { 1: "1, most critical", 2: "2", 3: "3, least critical" }, order: ["1", "2", "3"] },
];
const rosterState = { status: null, town: null, equipment: null, acuity: null, search: "" };
const rosterFilters = listFilters(ROSTER_FIELDS, rosterState, () => renderRoster(), {
  idPrefix: "roster", noun: "patients", searchText: (p) => `${p.name} ${p.address} ${p.device_id}` });
let rosterStatusKey = "";

function renderRoster() {
  if (!roster.length) return;
  rosterStatusKey = roster.map((p) => displayState(liveUnit(p.device_id))).join();
  const body = document.getElementById("roster-body");
  const urgency = (p) => rankIn(URGENT_FIRST)(displayState(liveUnit(p.device_id)));
  const shown = rosterFilters.mount(roster).sort((a, b) => urgency(a) - urgency(b) || a.acuity - b.acuity || a.name.localeCompare(b.name));
  if (!shown.length) { body.replaceChildren(noMatch("patients")); return; }
  const editing = can("manage_patients");
  body.replaceChildren(el("table", { class: "data-table stack-table" },
    el("thead", {}, el("tr", {}, ["Status", "Patient", "Equipment", "Acuity", "Call list", "Unit"].map((h) => el("th", { scope: "col" }, h)),
      el("th", { scope: "col" }, el("span", { class: "visually-hidden" }, "Actions")))),
    el("tbody", {}, shown.map((p) => el("tr", {},
      el("td", { "data-label": "Status" }, statusCell(liveUnit(p.device_id))),
      el("td", { "data-label": "Patient" }, el("div", { class: "cell-name" }, nameNode(p.name)), el("div", { class: "muted" }, p.address)),
      el("td", { "data-label": "Equipment" }, p.equipment, el("div", { class: "muted" }, `${durationText(p.backup_minutes)} of backup`)),
      el("td", { class: "num", "data-label": "Acuity" }, acuityBadge(p.acuity)),
      el("td", { class: "cell-wide", "data-label": "Call list" }, el("span", { class: "count-pill" }, String(p.contacts.length)),
        ` ${p.contacts.map((c) => c.name).join(", ")}`),
      el("td", { class: "code", "data-label": "Unit" }, p.device_id),
      el("td", { class: "cell-action" }, el("button", { class: "link-action", type: "button", "aria-label": `${editing ? "Edit" : "Details for"} ${nameParts(p.name)[0]}`,
        onclick: () => (editing ? openForm(p) : openDetails(p)) }, editing ? "Edit" : "Details")),
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
  historyIncidents = incidents;
  if (!incidents.length) {
    document.getElementById("history-tools").replaceChildren();
    body.replaceChildren(el("div", { class: "empty" },
      el("h2", {}, "Nothing to report"),
      el("p", { class: "muted" }, "Power outages and silent units will be listed here as they happen.")));
    return;
  }
  renderHistoryTable();
}

const RESPONSE = { answered: "Someone responded", none: "No one responded", open: "Still open", na: "Not needed" };
function responseKind(incident) {
  if (ADVISORY_KINDS.has(incident.kind)) return "na";
  if (incident.acknowledged_by) return "answered";
  return incident.ended_at === null ? "open" : "none";
}
const HISTORY_FIELDS = [
  { key: "kind", label: "Type", value: (i) => i.kind, names: KIND_LABEL, order: Object.keys(KIND_LABEL) },
  { key: "response", label: "Response", value: responseKind, names: RESPONSE, order: Object.keys(RESPONSE) },
  { key: "town", label: "Town", value: (i) => townFromAddress(liveUnit(i.device_id)?.address) },
];
const historyState = { kind: null, response: null, town: null, search: "" };
const historyFilters = listFilters(HISTORY_FIELDS, historyState, () => renderHistoryTable(), {
  idPrefix: "history", noun: "incidents", searchText: (i) => `${i.patient_name} ${i.device_id} ${i.detail ?? ""}` });
let historyIncidents = [];

function renderHistoryTable() {
  const body = document.getElementById("history-body");
  const shown = historyFilters.mount(historyIncidents);
  if (!shown.length) { body.replaceChildren(noMatch("incidents")); return; }
  body.replaceChildren(el("table", { class: "data-table stack-table" },
    el("thead", {}, el("tr", {}, ["Started", "Patient", "What happened", "Lasted", "Alerted", "Response"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, shown.map(historyRow))));
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
    el("td", { "data-label": "Patient" }, el("div", { class: "cell-name" }, nameNode(incident.patient_name)), el("div", { class: "muted code" }, incident.device_id)),
    el("td", { "data-label": "What happened" },
      kindChip(incident.kind),
      el("div", { class: "muted" }, incident.detail || incident.equipment)),
    el("td", { class: "num", "data-label": "Lasted" },
      ongoing ? (EMERGENCY_KINDS.has(incident.kind) ? chip("out", "Ongoing") : el("span", { class: "tag" }, "Ongoing")) : durationText(incident.duration_minutes)),
    el("td", { class: "cell-wide", "data-label": "Alerted" },
      incident.alerted.length ? listNames(incident.alerted.map((a) => a.name)) : "Nobody yet"),
    el("td", { class: "cell-wide", "data-label": "Response" }, responseText(incident)));
}

const KIND_STATE = { power: "out", machine: "out", help: "out", silent: "silent" };
function kindChip(kind) {
  return KIND_STATE[kind] ? chip(KIND_STATE[kind], KIND_LABEL[kind]) : el("span", { class: "tag tag-storm" }, KIND_LABEL[kind]);
}

function responseText(incident) {
  if (ADVISORY_KINDS.has(incident.kind)) return el("span", { class: "muted" }, "Not needed");
  if (incident.acknowledged_by) return el("span", { class: "response-ok" }, glyph("responding"), `${incident.acknowledged_by} after ${durationText(incident.response_minutes)}`);
  if (incident.ended_at === null && !incident.acknowledged_by) return el("span", { class: "response-wait" }, "Waiting");
  return el("span", { class: "no-response" }, glyph("unreached"), "No one responded");
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

const UNIT_FIELDS = [
  { key: "status", label: "Status", value: displayState, names: DISPLAY_WORD, order: SILENT_FIRST },
  { key: "town", label: "Town", value: (u) => townFromAddress(u.address) },
];
const unitState = { status: null, town: null, search: "" };
const unitFilters = listFilters(UNIT_FIELDS, unitState, () => renderUnits(), {
  idPrefix: "units", noun: "units", searchText: (u) => `${u.name} ${u.address} ${u.device_id}` });

function renderUnits() {
  const body = document.getElementById("units-body");
  if (!lastUnits.length) {
    body.replaceChildren(el("div", { class: "empty" }, el("h2", {}, "No units yet"),
      el("p", { class: "muted" }, "Add a patient and set up their power unit to see it here.")));
    return;
  }
  const health = (u) => rankIn(SILENT_FIRST)(displayState(u));
  const units = unitFilters.mount(lastUnits).sort((a, b) => health(a) - health(b) || a.name.localeCompare(b.name));
  if (!units.length) { body.replaceChildren(noMatch("units")); return; }
  body.replaceChildren(el("table", { class: "data-table stack-table" },
    el("thead", {}, el("tr", {}, ["Unit", "Status", "Last report", "Battery", "Wi-Fi", "Sensors", "Firmware", "Running for"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, units.map((unit) => el("tr", {},
      el("td", { "data-label": "Unit" }, el("div", { class: "cell-name" }, nameNode(unit.name)), el("div", { class: "muted code" }, unit.device_id)),
      el("td", { "data-label": "Status" }, statusCell(unit),
        unit.status === "offline" ? el("div", { class: "muted" }, `silent for ${durationText(minutesSince(unit.last_seen))}`) : null),
      el("td", { class: "num", "data-label": "Last report" }, unit.last_seen ? secondsAgo(unit.last_seen) : el("span", { class: "muted" }, "Never connected")),
      el("td", { class: "num", "data-label": "Battery" }, batteryCell(unit.battery_percent)),
      el("td", { "data-label": "Wi-Fi" }, signalCell(unit.rssi)),
      el("td", { "data-label": "Sensors" }, sensorsText(unit)),
      el("td", { class: "code", "data-label": "Firmware" }, unit.firmware ?? el("span", { class: "muted" }, "Unknown")),
      el("td", { class: "num", "data-label": "Running for" }, unit.uptime_s === null ? el("span", { class: "muted" }, "Unknown") : uptimeText(unit.uptime_s)),
    )))));
}

// The unit's own battery (not the patient's equipment backup): ink, with "low" in words under 20 %.
function batteryCell(percent) {
  if (percent === null || percent === undefined) return el("span", { class: "muted" }, "Not reported");
  const meter = el("span", { class: "battery", "aria-hidden": "true" }, el("span"));
  meter.firstChild.style.width = `${Math.max(4, Math.min(100, percent))}%`;
  return el("span", { class: `battery-cell${percent < 20 ? " is-low" : ""}` }, meter, `${percent}%${percent < 20 ? ", low" : ""}`);
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
  // Roles are free text on the server and decide who counts as care staff or county, so a saved role
  // that isn't one of the choices ("agency", "caregiver") must survive an edit, not turn into "Family".
  const savedRole = contact.role?.trim() ?? "";
  const knownRole = ROLES.find((r) => r.toLowerCase() === savedRole.toLowerCase());
  const roleChoices = savedRole && !knownRole ? [...ROLES, savedRole] : ROLES;
  const move = (step) => {
    const sibling = step < 0 ? row.previousElementSibling : row.nextElementSibling;
    if (!sibling) return;
    row.parentElement.insertBefore(row, step < 0 ? sibling : sibling.nextElementSibling);
    renumber();
    row.querySelector(step < 0 ? "[data-move=up]" : "[data-move=down]")?.focus();
  };
  const row = el("div", { class: "contact-row" },
    el("span", { class: "contact-order", "aria-hidden": "true" }),
    field("Name", unique(input("c-name", contact.name, { required: true, autocomplete: "off" }))),
    field("Role", unique(select("c-role", roleChoices.map((r) => [r, r]), knownRole || savedRole || "Family"))),
    field("Phone", unique(input("c-phone", contact.phone, { type: "tel", placeholder: "+1 601 555 0123", required: true })), null, "field field-wide"),
    field("Reach by", unique(select("c-channel", [["sms", "Text"], ["call", "Phone call"]], contact.channel || "sms"))),
    el("div", { class: "contact-tools" },
      el("button", { class: "link-action", type: "button", "data-move": "up", onclick: () => move(-1) }, "Move up"),
      el("button", { class: "link-action", type: "button", "data-move": "down", onclick: () => move(1) }, "Move down"),
      el("button", { class: "link-action is-danger", type: "button", "data-remove": "", onclick: () => { row.remove(); renumber(); } }, "Remove")),
  );
  return row;
}

// Order numbers, and labels that say which person each button moves or removes.
function renumber() {
  const rows = [...panelContent.querySelectorAll(".contact-row")];
  rows.forEach((row, index) => {
    row.querySelector(".contact-order").textContent = `${index + 1}`;
    const who = row.querySelector("[name=c-name]").value.trim() || `person ${index + 1}`;
    row.querySelector("[data-move=up]").disabled = index === 0;
    row.querySelector("[data-move=down]").disabled = index === rows.length - 1;
    row.querySelector("[data-move=up]").setAttribute("aria-label", `Move ${who} up the call list`);
    row.querySelector("[data-move=down]").setAttribute("aria-label", `Move ${who} down the call list`);
    row.querySelector("[data-remove]").setAttribute("aria-label", `Remove ${who} from the call list`);
  });
}

function openForm(patient) {
  const editing = Boolean(patient);
  document.getElementById("panel-title").textContent = editing ? `Edit ${patient.name}` : "Add patient";
  const contacts = el("div", { id: "contacts", class: "contacts" }, (patient?.contacts?.length ? patient.contacts : [{}]).map(contactRow));
  const error = el("p", { class: "form-error", role: "alert", hidden: true });
  const save = el("button", { class: "btn btn-primary", type: "submit" }, editing ? "Save changes" : "Add patient");

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
      el("button", { class: "btn btn-quiet", type: "button", onclick: () => { contacts.append(contactRow()); renumber(); contacts.lastElementChild.querySelector("input").focus(); } }, "Add contact")),
    el("div", { class: "panel-actions" },
      save,
      el("button", { class: "btn btn-quiet", type: "button", onclick: () => panel.close() }, "Cancel"),
      editing ? removeButton(patient) : null),
  );

  // A corrected field stops being marked as wrong as soon as it is edited.
  form.addEventListener("input", (event) => { event.target.removeAttribute("aria-invalid"); if (event.target.name === "c-name") renumber(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    form.querySelectorAll("[aria-invalid]").forEach((node) => node.removeAttribute("aria-invalid"));
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
      form.querySelector("[aria-invalid=true]")?.focus({ preventScroll: true });
    }
  });

  panelContent.replaceChildren(form);
  if (editing) panelContent.append(credentialsBlock(patient), recentIncidentsBlock(patient));
  renumber();
  panel.showModal();
  form.querySelector("input:not([disabled])").focus();
}

// Read only, for care staff: how to reach the patient and their people, and what the plan says.
/** Facts, call list and emergency plan: the same blocks in the side panel and in an opened board row. */
function patientDetails(patient) {
  const plan = patient.plan ?? {};
  const fact = (label, value) => (value || value === 0) ? el("div", {}, el("dt", {}, label), el("dd", {}, String(value))) : null;
  return [
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
  ];
}

function openDetails(patient) {
  document.getElementById("panel-title").textContent = patient.name;
  panelContent.replaceChildren(
    ...patientDetails(patient),
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
  body.replaceChildren(el("table", { class: "data-table stack-table" },
    el("thead", {}, el("tr", {}, ["Email", "Role"].map((h) => el("th", { scope: "col" }, h)), el("th", { scope: "col" }, el("span", { class: "visually-hidden" }, "Actions")))),
    el("tbody", {}, staff.map(staffRow))));
}

function staffRow(person) {
  const isMe = person.email === me.email;
  const role = el("select", { class: "input input-small", name: "role", "aria-label": `Role for ${person.email}`, disabled: isMe },
    roleOptions.map((r) => el("option", { value: r.value, selected: r.value === person.role }, r.label)));
  const note = el("span", { class: "form-error", role: "alert", hidden: true });
  role.addEventListener("change", async () => {
    note.hidden = true;
    try {
      await guarded(api("PUT", "/api/staff", { email: person.email, role: role.value }));
      person.role = role.value;  // a later refused change falls back to this, not to the role on first load
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
      remove.textContent = `Confirm: remove ${person.email}`;
      // Two steps, no browser dialog; an unconfirmed tap quietly disarms.
      setTimeout(() => { if (remove.isConnected && remove.dataset.confirm === "yes") { delete remove.dataset.confirm; remove.textContent = "Remove access"; } }, 6000);
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
    el("td", { class: "cell-action" }, remove));
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
       el("p", { class: "hint muted" }, "This password is shown only once. If it is lost, add them again with the same email for a new one.")]
    // No temporary password only happens in local mode, where everyone shares LIFELINE_ADMIN_PASSWORD.
    : [el("p", {}, `This Lifeline uses one shared staff password (local mode), so there is no account to make. They sign in at ${location.origin}/login with that password, as ${label}.`)];
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

/** "601 555 0123", "(601) 555-0123" or "+1 601-555-0123" -> "+16015550123"; "" when it can't be a phone number.
    The server only accepts the + form, and staff type numbers the way they say them. */
function toE164(text) {
  const digits = text.replace(/\D/g, "");
  if (text.trim().startsWith("+")) return digits.length >= 8 && digits.length <= 15 ? `+${digits}` : "";
  if (digits.length === 10) return `+1${digits}`;
  if (digits.length === 11 && digits.startsWith("1")) return `+${digits}`;
  return "";
}

// Checks what the server would refuse, and says which field, so nobody hunts for the problem.
function readForm(form) {
  const value = (name) => form.elements[name].value.trim();
  const problems = [];
  const flag = (control, message) => { control.setAttribute("aria-invalid", "true"); problems.push(message); };
  const labelOf = (name) => form.querySelector(`label[for="${form.elements[name].id}"]`)?.textContent ?? name;
  for (const name of ["name", "address", "lat", "lon", "equipment", "backup_minutes", "device_id"]) {
    if (!form.elements[name].disabled && !value(name)) flag(form.elements[name], `${labelOf(name)} is empty`);
  }
  const number = (name, low, high, words) => {
    if (value(name) === "") return;
    const n = Number(value(name));
    if (!Number.isFinite(n) || n < low || n > high) flag(form.elements[name], `${labelOf(name)} must be ${words}`);
  };
  number("lat", -90, 90, "a number between -90 and 90");
  number("lon", -180, 180, "a number between -180 and 180");
  number("backup_minutes", 0, 2880, "whole minutes from 0 to 2880");
  if (value("running_amps_min") !== "") number("running_amps_min", 0, 100, "a number of amps, or blank");
  const unitId = form.elements.device_id;
  if (!unitId.disabled && value("device_id") && !/^[a-z0-9-]{3,40}$/.test(value("device_id"))) {
    flag(unitId, "Unit ID must be 3 to 40 lowercase letters, digits or dashes");
  }
  const rows = [...form.querySelectorAll(".contact-row")];
  if (!rows.length) problems.push("Add at least one person to the call list");
  const contacts = rows.map((row, index) => {
    const name = row.querySelector("[name=c-name]");
    const phone = row.querySelector("[name=c-phone]");
    if (!name.value.trim()) flag(name, `Call list person ${index + 1} needs a name`);
    if (!phone.value.trim()) flag(phone, `Call list person ${index + 1} needs a phone number`);
    else if (!(phone.value = toE164(phone.value))) flag(phone, `Call list person ${index + 1}'s phone number should be 10 digits, or start with + and a country code`);
    return { name: name.value.trim(), role: row.querySelector("[name=c-role]").value, phone: phone.value.trim(), channel: row.querySelector("[name=c-channel]").value };
  });
  if (problems.length) throw new Error(`Check ${problems.length === 1 ? "this" : "these"} before saving: ${problems.join("; ")}.`);
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
    try {
      await withButton(button, () => guarded(api("DELETE", `/api/patients/${encodeURIComponent(patient.device_id)}`)));
    } catch (error) {
      button.textContent = error.message;
      return;
    }
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
    let updated;
    try {
      updated = await withButton(rotate, () => guarded(api("POST", `/api/patients/${encodeURIComponent(patient.device_id)}/rotate-link`, {})));
    } catch (error) {
      rotate.textContent = error.message;
      return;
    }
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

// ---- demo board only: the storm simulation -----------------------------------------
// The server answers 404 here unless it was started with LIFELINE_STORM_SIM=1, so on the real
// board this panel never appears. Every home it touches is synthetic; the server checks.

const simPanel = document.getElementById("sim-panel");
const simForm = document.getElementById("sim-form");
const simStatus = document.getElementById("sim-status");
let simPolling = null;

function showSim(state) {
  const first = simPanel.hidden;
  simPanel.hidden = false;
  simForm.homes.max = String(state.homes);
  if (first) simForm.homes.value = String(Math.min(16, state.homes));
  simStatus.textContent = state.running && state.out < state.planned ? `Storm running: ${state.out} of ${state.planned} homes out so far.`
    : state.out ? `${plural(state.out, "simulated home")} without power.` : "";
}

async function refreshSim() {
  try {
    showSim(await api("GET", "/api/sim"));
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) clearInterval(simPolling);  // the simulation is off here
  }
}

async function simAction(button, path, body) {
  try {
    showSim(await withButton(button, () => guarded(api("POST", path, body))));
  } catch (error) {
    simStatus.textContent = error.message;
  }
  refreshBoard();
}

simForm.addEventListener("submit", (event) => {
  event.preventDefault();
  simAction(simForm.querySelector("button[type=submit]"), "/api/sim/storm",
    { homes: Number(simForm.homes.value), every_seconds: Number(simForm.every.value) });
});
document.getElementById("sim-calm").addEventListener("click", (event) => simAction(event.currentTarget, "/api/sim/calm", {}));
if (can("manage_patients")) {
  refreshSim();
  simPolling = setInterval(refreshSim, BOARD_REFRESH_MS);
}

// ---- start -----------------------------------------------------------------------

startClock(document.getElementById("view-board"));

showView();
refreshBoard();
setInterval(refreshBoard, BOARD_REFRESH_MS);
refreshWeather();
setInterval(refreshWeather, WEATHER_REFRESH_MS);
