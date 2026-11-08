// Action cards: one per home that needs someone now. Each card answers, without scrolling,
// what happened, how long ago, how much backup is left, who has been told, who is next,
// and gives the actions as buttons. Times tick in place (see startClock); cards are only
// rebuilt when their own data changes, so a tap is never lost to a refresh.
import { el, backupTone, chip, clockTime, glyph, hazardText, homeState, nameNode, runsOutAt, STATE_WORD } from "./common.js";
import { filterBar } from "./filters.js";

const WHAT = { power: "Power out", machine: "Machine stopped", help: "Help button pressed" };

// ---- live clocks ------------------------------------------------------------------

export function clockText(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** A span that shows time since (data-since) or until (data-until) an instant, updated every second. */
export function liveTime(kind, iso) {
  return el("span", { class: "live-time", [`data-${kind}`]: iso }, "");
}

const TONES = ["ink", "amber", "red"];

function tick(node) {
  const now = Date.now();
  if (node.dataset.since) node.textContent = clockText(now - Date.parse(node.dataset.since));
  if (!node.dataset.until) return;
  const left = Date.parse(node.dataset.until) - now;
  const backup = node.closest(".backup-clock, .row-clock");
  // A backup clock at zero says so in words; other countdowns (next alert) just stop at 0:00.
  node.textContent = left <= 0 && backup ? "Out now" : clockText(left);
  if (!backup) return;
  const tone = backupTone(left);
  if (backup.dataset.tone !== tone) {
    backup.classList.remove(...TONES.map((t) => `tone-${t}`));
    backup.classList.add(`tone-${tone}`);
    backup.dataset.tone = tone;
  }
  if (backup.dataset.total) backup.style.setProperty("--left", String(Math.max(0, Math.min(1, left / Number(backup.dataset.total)))));
  const label = backup.querySelector(".clock-label");
  if (label) label.textContent = left <= 0 ? "backup gone" : "backup left";
}

/** Fill every live time under root now, rather than at the next clock tick. */
export function tickAll(root) {
  root.querySelectorAll(".live-time").forEach(tick);
}

export function startClock(root = document) {
  const run = () => root.querySelectorAll(".live-time").forEach(tick);
  run();
  return setInterval(run, 1000);
}

// ---- card parts ---------------------------------------------------------------------

function backupClock(unit) {
  const total = unit.backup_minutes * 60000;
  if (!total) return el("div", { class: "backup-clock tone-red" }, el("p", { class: "backup-clock-label" }, "Backup left"),
    el("p", { class: "backup-clock-value" }, "None"), el("p", { class: "backup-clock-note" }, "The equipment stopped with the power."));
  const runsOut = runsOutAt(unit);
  const clock = el("div", { class: "backup-clock", "data-total": String(total) },
    el("p", { class: "backup-clock-label" }, "Backup left"),
    el("p", { class: "backup-clock-value" }, liveTime("until", runsOut)),
    el("div", { class: "backup-bar", "aria-hidden": "true" }, el("span")),
    el("p", { class: "backup-clock-note" }, `Runs out around ${clockTime(runsOut)}`));
  tick(clock.querySelector(".live-time"));
  return clock;
}

const step = (icon, ...content) => el("li", { class: `step is-${icon}` }, ...content);

function timeline(unit) {
  const steps = [step("out", el("strong", {}, WHAT[unit.emergency] ?? "Power out"), " ", liveTime("since", unit.outage_started), " ago")];
  const alerted = unit.contacts_alerted_at?.length ? unit.contacts_alerted_at : unit.contacts_alerted.map((name) => ({ name, at: null }));
  alerted.forEach(({ name, at }, index) => {
    const unanswered = !unit.acknowledged_by && (index < alerted.length - 1 || unit.unreached);
    steps.push(step("done", el("strong", {}, `Alerted ${name}`), at ? [" ", liveTime("since", at), " ago"] : null, unanswered ? ", no answer" : null));
  });
  if (unit.acknowledged_by) {
    steps.push(step("claimed", el("strong", {}, `${claimedBy(unit)} responded`), unit.acknowledged_at ? ` at ${clockTime(unit.acknowledged_at)}` : null));
  } else if (unit.next_contact) {
    steps.push(step("next", el("strong", {}, `Next alert to ${unit.next_contact.name}`), " in ", liveTime("until", unit.next_alert_at)));
  }
  if (unit.critical && !unit.acknowledged_by) steps.push(step("told", el("strong", {}, "County and care staff told"), ": call 911 if nobody is there"));
  if (unit.backup_low && !unit.critical) steps.push(step("told", el("strong", {}, "Backup almost out"), ": the whole call list was told"));
  return el("ol", { class: "incident-steps" }, steps);
}

/** Who has taken this home, short: staff by the name part of their email, family as they replied. */
export function claimedBy(unit) {
  const by = unit.acknowledged_by ?? "";
  return by.endsWith(" (agency)") ? by.replace(" (agency)", "").split("@")[0] : by;
}

export function townOf(unit) {
  return unit.address.split(",").slice(-2, -1)[0]?.trim() || "Unknown town";
}

const ICON = { phone: "M13.8 16.6a1 1 0 0 0 1.2-.3l.4-.5A2 2 0 0 1 17 15h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2A18 18 0 0 1 2 4a2 2 0 0 1 2-2h3a2 2 0 0 1 2 2v3a2 2 0 0 1-.8 1.6l-.5.4a1 1 0 0 0-.3 1.2 14 14 0 0 0 6.4 6.4",
  hand: "M18 11V6a2 2 0 0 0-4 0v1M14 10V4a2 2 0 0 0-4 0v2M10 10.5V6a2 2 0 0 0-4 0v8M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.9-6-2.4l-3.6-3.6a2 2 0 0 1 2.8-2.8L7 15",
  nav: "M3 11l19-9-9 19-2-8-8-2z", plan: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h8",
  list: "M3 5h.01M3 12h.01M3 19h.01M8 5h13M8 12h13M8 19h13" };

/** A small stroked icon (Lucide shapes), decorative: every button also has its words. */
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON[name]);
  svg.append(path);
  return svg;
}

function call911() {
  return el("a", { class: "btn btn-911", href: "tel:911" }, icon("phone"), "Call 911");
}

function callButton(contact, label, className = "btn btn-call") {
  return el("a", { class: className, href: `tel:${contact.phone}` }, icon("phone"), label ?? `Call ${contact.name}`);
}

const CARE_STAFF = /agency|nurse|home health/i;  // same words the server uses for care staff

// Who to phone: whoever claimed it (if they are on the list), the person the escalation reaches
// next, then care staff, then the first person on the list.
function callTargets(unit) {
  if (!unit.contacts?.length) return [];
  const claimer = unit.acknowledged_by && unit.contacts.find((c) => unit.acknowledged_by.startsWith(c.name));
  const next = !unit.acknowledged_by && unit.next_contact && unit.contacts.find((c) => c.name === unit.next_contact.name);
  const staff = unit.contacts.find((c) => CARE_STAFF.test(c.role));
  return [...new Set([claimer, next, staff, unit.contacts[0]].filter(Boolean))];
}

// Short enough for a row button: care staff are "nurse", family keep their name ("Wife", "Son").
function shortName(contact) {
  return CARE_STAFF.test(contact.role) ? "nurse" : contact.name;
}

function directionsLink(unit, className = "link-action") {
  return el("a", { class: className, href: `https://www.google.com/maps/dir/?api=1&destination=${unit.lat},${unit.lon}`,
    target: "_blank", rel: "noopener noreferrer" }, icon("nav"), "Directions");
}

// Plan and call list open in place; which ones are open survives a card rebuild.
const openExtras = new Set();

function extraToggle(unit, kind, label, panel) {
  const key = `${unit.device_id}|${kind}`;
  panel.hidden = !openExtras.has(key);
  panel.id = `extra-${kind}-${unit.device_id}`;
  const button = el("button", { class: "link-action", type: "button", "aria-expanded": String(openExtras.has(key)), "aria-controls": panel.id },
    icon(kind === "plan" ? "plan" : "list"), label);
  button.addEventListener("click", () => {
    const open = !openExtras.has(key);
    if (open) openExtras.add(key); else openExtras.delete(key);
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  });
  return button;
}

function planPanel(unit) {
  const plan = unit.plan ?? {};
  if (!Object.values(plan).some(Boolean)) return null;
  const place = [plan.place_name, plan.place_address].filter(Boolean).join(", ");
  return el("div", { class: "card-extra" },
    plan.supplies ? el("p", {}, el("strong", {}, "Supplies: "), plan.supplies) : null,
    place ? el("p", {}, el("strong", {}, "Go to: "), place, plan.place_phone ? " " : null,
      plan.place_phone ? el("a", { href: `tel:${plan.place_phone}` }, plan.place_phone) : null) : null,
    plan.steps ? el("p", { class: "plan-steps" }, plan.steps) : null);
}

function callListPanel(unit) {
  if (!unit.contacts?.length) return null;
  return el("div", { class: "card-extra" }, el("ol", {}, unit.contacts.map((c) => el("li", {},
    el("a", { href: `tel:${c.phone}` }, c.name), el("span", { class: "muted" }, ` ${c.role}, ${c.phone}`)))));
}

// ---- cards ---------------------------------------------------------------------------

export function needsAction(unit) {
  return unit.status === "outage" || unit.status === "offline";
}

function wordFor(unit, state) {
  if (state === "silent") return "Unit silent";
  if (state === "out" && unit.emergency && unit.emergency !== "power") return WHAT[unit.emergency];
  return STATE_WORD[state];
}

/** One row of two equal buttons: Call 911 when it is red, else the next call; and I'm responding. */
function primaryRow(unit, state, { respondButton, canRespond }) {
  const targets = callTargets(unit);
  const left = state === "critical" || state === "unreached" ? call911()
    : targets[0] ? callButton(targets[0], `Call ${shortName(targets[0])}`) : null;
  const right = canRespond && unit.status === "outage" && !unit.acknowledged_by ? respondButton(unit) : null;
  return el("div", { class: "card-primary" }, left, right);
}

function linkRow(unit, state, panels) {
  const targets = callTargets(unit);
  const usedFirst = !(state === "critical" || state === "unreached");
  const calls = targets.slice(usedFirst ? 1 : 0, usedFirst ? 3 : 2);
  return el("div", { class: "card-links" },
    calls.map((c) => callButton(c, `Call ${shortName(c)}`, "link-action")),
    directionsLink(unit),
    panels.plan ? extraToggle(unit, "plan", "Emergency plan", panels.plan) : null,
    panels.list ? extraToggle(unit, "list", `Call list · ${unit.contacts.length}`, panels.list) : null);
}

export function actionCard(unit, { respondButton, canRespond = true, onLocate }) {
  const state = homeState(unit);
  const town = townOf(unit);
  const head = el("div", { class: "card-head" },
    el("h2", {}, el("button", { class: "linklike", type: "button", onclick: () => onLocate(unit), title: "Show on the map" }, nameNode(unit.name))),
    el("p", { class: "card-meta" }, chip(state, wordFor(unit, state)), ` ${town} · ${unit.equipment}, acuity ${unit.acuity}`));
  const panels = { plan: planPanel(unit), list: callListPanel(unit) };

  if (unit.status === "offline") {
    return el("article", { class: `unit action-card is-${state}`, "data-device": unit.device_id }, head,
      el("p", { class: "card-note" }, "No report for ", liveTime("since", unit.last_seen),
        ". The unit's Wi-Fi or battery may be down, or the power may be out. Check on the patient."),
      primaryRow(unit, state, { respondButton, canRespond }), linkRow(unit, state, panels), panels.plan, panels.list);
  }

  return el("article", { class: `unit action-card is-${state}`, "data-device": unit.device_id },
    head,
    el("div", { class: "card-body" },
      unit.emergency === "help" ? null : backupClock(unit),
      el("div", { class: "card-facts" }, timeline(unit),
        ...unit.hazards.map((h) => el("p", { class: "hazard-chip" }, `${hazardText(h)}`)))),
    primaryRow(unit, state, { respondButton, canRespond }),
    linkRow(unit, state, panels),
    panels.plan, panels.list);
}

// ---- keyed rendering -----------------------------------------------------------------

// Heartbeat readings change every few seconds; a card is only rebuilt when something a person acts on changes.
const VOLATILE = new Set(["last_seen", "battery_percent", "rssi", "uptime_s", "room_temp_f", "load_amps", "backup_minutes_left", "conditions"]);
const stable = (item) => JSON.stringify(item, (key, value) => (VOLATILE.has(key) ? undefined : value));

/** Replace only the children whose data changed, keyed by device id, in the given order. */
export function renderKeyed(container, items, build) {
  const existing = new Map([...container.children].map((node) => [node.dataset.key, node]));
  const wanted = items.map((item) => {
    const key = item.device_id;
    const json = item.status === "ok" ? JSON.stringify(item) : stable(item);
    const old = existing.get(key);
    if (old && old.dataset.json === json) return old;
    const node = build(item);
    node.dataset.key = key;
    node.dataset.json = json;
    return node;
  });
  wanted.forEach((node, index) => {
    if (container.children[index] !== node) container.insertBefore(node, container.children[index] ?? null);
  });
  while (container.children.length > wanted.length) container.lastElementChild.remove();
  container.querySelectorAll(".live-time:empty").forEach(tick);  // new rows show their times now, not at the next clock tick
}

// ---- situation at a glance: six counts, readable from across the room -------------------

const COUNTS = [
  { key: "critical", test: (u) => homeState(u) === "critical", label: "under 15 min left" },
  { key: "unreached", test: (u) => u.status === "outage" && u.unreached && !u.acknowledged_by, label: "nobody answering" },
  { key: "low", test: (u) => u.status === "outage" && u.backup_low, label: "backup almost out" },
  { key: "responding", test: (u) => homeState(u) === "responding", label: "someone responding" },
  { key: "silent", test: (u) => u.status === "offline", label: "unit silent" },
  { key: "storm", test: (u) => u.hazards.length > 0, label: "in the storm's path" },
];

export function countsOf(units) {
  return COUNTS.map((kind) => ({ ...kind, matches: units.filter(kind.test) }));
}

/** The 2 × 3 count grid; only "under 15 min" is a white cell. A tap jumps to the first home it counts. */
export function countGrid(units, onPick) {
  return el("div", { class: "count-grid" }, countsOf(units).map(({ key, label, matches }) =>
    el("button", { class: `count-cell is-${key}`, type: "button", disabled: !matches.length, onclick: () => onPick(matches[0]) },
      el("span", { class: "count-number" }, String(matches.length), key === "critical" ? glyph("critical") : null),
      el("span", { class: "count-label" }, label))));
}

/** Calm board: the few counts that exist, as quiet chips (storm path, silent units). */
export function situationChips(units, onPick) {
  const chips = countsOf(units).filter((c) => c.matches.length);
  if (!chips.length) return null;
  return el("div", { class: "situation", role: "list" }, chips.map(({ key, label, matches }) =>
    el("button", { class: `chip is-${key}`, type: "button", role: "listitem", onclick: () => onPick(matches[0]) }, `${matches.length} ${label}`)));
}

// ---- triage mode: many homes at once -------------------------------------------------

export const TRIAGE_AT = 4;  // from this many homes needing someone, cards fold into a ranked list

const URGENCY = (u) => (u.status === "offline" ? 3 : u.critical ? -1 : u.acknowledged_by ? 2 : u.unreached ? 0 : 1);

/** Homes that need someone, most urgent first: under 15 min, nobody answering, then waiting, then
    someone responding, then silent units; within each, the least backup left first. The order holds
    when someone claims a home, so a row never jumps away from a nurse's cursor. */
export function triageOrder(units) {
  const left = (u) => (u.status === "outage" && u.backup_minutes ? Date.parse(u.outage_started) + u.backup_minutes * 60000 : Infinity);
  return units.filter(needsAction).sort((a, b) => URGENCY(a) - URGENCY(b) || left(a) - left(b));
}

function rowClock(unit) {
  if (unit.status === "offline") {
    return el("span", { class: "row-clock is-quiet" }, el("span", { class: "clock-value" }, liveTime("since", unit.last_seen)),
      el("span", { class: "clock-label" }, "since last report"));
  }
  if (unit.emergency === "help") return el("span", { class: "row-clock" }, el("span", { class: "clock-value" }, "Help"), el("span", { class: "clock-label" }, "button pressed"));
  if (!unit.backup_minutes) return el("span", { class: "row-clock tone-red" }, el("span", { class: "clock-value" }, "Out now"), el("span", { class: "clock-label" }, "no backup"));
  return el("span", { class: "row-clock" }, el("span", { class: "clock-value" }, liveTime("until", runsOutAt(unit))), el("span", { class: "clock-label" }, "backup left"));
}

function rowMeta(unit) {
  if (unit.acknowledged_by) return `${claimedBy(unit)}${unit.acknowledged_at ? ` · since ${clockTime(unit.acknowledged_at)}` : ""}`;
  return `${townOf(unit)} · ${unit.equipment}`;
}

/** One line per home: shape and rank, who and where, time left, and the single most useful action.
    Tapping the name opens the full card underneath. */
export function triageRow(unit, rank, { respondButton, canRespond, open, onToggle, onLocate }) {
  const state = homeState(unit);
  const target = callTargets(unit)[0];
  const primary = state === "critical" ? call911()
    : canRespond && !unit.acknowledged_by && unit.status === "outage" ? respondButton(unit)
    : target ? callButton(target, `Call ${shortName(target)}`) : null;
  const line = el("div", { class: "triage-line" },
    el("span", { class: "rank" }, glyph(state), el("span", { class: "rank-number" }, String(rank))),
    el("div", { class: "triage-main" },
      el("button", { class: "triage-who", type: "button", "aria-expanded": String(open), onclick: () => onToggle(unit) },
        nameNode(unit.name), el("span", { class: "triage-chevron", "aria-hidden": "true" }, open ? "▾" : "▸")),
      el("p", { class: "triage-meta" }, chip(state, wordFor(unit, state)), el("span", {}, rowMeta(unit)))),
    rowClock(unit),
    primary);
  const card = open ? actionCard(unit, { respondButton, canRespond, onLocate }) : null;
  card?.classList.add("is-expanded");
  return el("article", { class: `unit triage-row is-${state}${open ? " is-open" : ""}`, "data-device": unit.device_id }, line, card);
}

// ---- dispatch tools for triage mode -------------------------------------------------

// One status per home for filtering, in the same words the rows use.
const STATUS_FILTERS = [
  ["critical", "Under 15 min"], ["unreached", "No answer"], ["waiting", "Power out"],
  ["claimed", "Responding"], ["silent", "Unit silent"],
];
export function statusOf(unit) {
  if (unit.status === "offline") return "silent";
  if (unit.acknowledged_by) return "claimed";
  if (unit.critical) return "critical";
  return unit.unreached ? "unreached" : "waiting";
}

export const TRIAGE_FIELDS = [
  { key: "town", label: "Town", value: townOf },
  { key: "status", label: "Status", value: statusOf, names: Object.fromEntries(STATUS_FILTERS), order: STATUS_FILTERS.map(([key]) => key) },
  { key: "equipment", label: "Equipment", value: (u) => u.equipment },
];

/** The shared filter bar for the triage list. */
export function triageTools(active, { state, onChange, onReset }) {
  return filterBar(active, TRIAGE_FIELDS, state, { onChange, onReset, idPrefix: "triage", noun: "homes" });
}
