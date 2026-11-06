// Action cards: one per home that needs someone now. Each card answers, without scrolling,
// what happened, how long ago, how much backup is left, who has been told, who is next,
// and gives the actions as buttons. Times tick in place (see startClock); cards are only
// rebuilt when their own data changes, so a tap is never lost to a refresh.
import { el, clockTime, listNames, stateOf } from "./common.js";
import { escalationNotes } from "./escalation.js";
import { filterBar } from "./filters.js";

const WHAT = { power: "Power out", machine: "Machine stopped", help: "Help button pressed" };
const URGENT_MINUTES = 60;

// ---- live clocks ------------------------------------------------------------------

function clockText(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** A span that shows time since (data-since) or until (data-until) an instant, updated every second. */
function liveTime(kind, iso) {
  return el("span", { class: "live-time", [`data-${kind}`]: iso }, "");
}

function tick(node) {
  const now = Date.now();
  if (node.dataset.since) node.textContent = clockText(now - Date.parse(node.dataset.since));
  if (node.dataset.until) {
    const left = Date.parse(node.dataset.until) - now;
    // A backup clock at zero says so in words; other countdowns (next alert) just stop at 0:00.
    node.textContent = left <= 0 && node.closest(".backup-clock, .row-clock") ? "Out now" : clockText(left);
  }
  const meter = node.closest(".backup-clock");
  if (meter && node.dataset.until) {
    const left = Date.parse(node.dataset.until) - now;
    meter.style.setProperty("--left", String(Math.max(0, Math.min(1, left / Number(meter.dataset.total)))));
    meter.classList.toggle("is-urgent", left < URGENT_MINUTES * 60000);
    meter.classList.toggle("is-empty", left <= 0);
  }
}

export function startClock(root = document) {
  const run = () => root.querySelectorAll(".live-time").forEach(tick);
  run();
  return setInterval(run, 1000);
}

// ---- card parts ---------------------------------------------------------------------

function backupClock(unit) {
  const total = unit.backup_minutes * 60000;
  if (!total) return el("p", { class: "backup-clock is-empty" }, el("strong", {}, "No backup"), " The equipment stopped with the power.");
  const runsOut = new Date(Date.parse(unit.outage_started) + total).toISOString();
  const clock = el("div", { class: "backup-clock", "data-total": String(total) },
    el("p", { class: "backup-clock-label" }, "Backup left"),
    el("p", { class: "backup-clock-value" }, liveTime("until", runsOut)),
    el("div", { class: "backup-bar", "aria-hidden": "true" }, el("span")),
    el("p", { class: "backup-clock-note" }, `Runs out around ${clockTime(runsOut)} if the power stays off.`));
  tick(clock.querySelector(".live-time"));
  return clock;
}

function timeline(unit) {
  const steps = [
    el("li", { class: "is-done" }, el("strong", {}, WHAT[unit.emergency] ?? "Power out"), " ", liveTime("since", unit.outage_started), " ago"),
  ];
  if (unit.contacts_alerted.length) steps.push(el("li", { class: "is-done" }, `Alerted ${listNames(unit.contacts_alerted)}`));
  if (unit.acknowledged_by) {
    steps.push(el("li", { class: "is-handled" }, el("strong", {}, "Claimed by "), claimedBy(unit)));
  } else if (unit.next_contact) {
    steps.push(el("li", { class: "is-next" }, `Next alert to ${unit.next_contact.name} in `, liveTime("until", unit.next_alert_at)));
  }
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

function call911() {
  return el("a", { class: "btn btn-911", href: "tel:911" }, "Call 911");
}

function callButton(contact, label) {
  return el("a", { class: "btn btn-call", href: `tel:${contact.phone}` }, label ?? `Call ${contact.name}`);
}

const CARE_STAFF = /agency|nurse|home health/i;  // same words the server uses for care staff

// Who to phone first: the person the escalation reaches next; once everyone has been alerted
// (or someone is responding), care staff first, then the first person on the list.
function callTargets(unit) {
  if (!unit.contacts?.length) return [];
  const next = unit.next_contact && unit.contacts.find((c) => c.name === unit.next_contact.name);
  if (next) return [next];
  const staff = unit.contacts.find((c) => CARE_STAFF.test(c.role));
  return [...new Set([staff, unit.contacts[0]].filter(Boolean))];
}

// Short enough for a row button: care staff are "nurse", family keep their name ("Wife", "Son (call)").
function shortName(contact) {
  return CARE_STAFF.test(contact.role) ? "nurse" : contact.name;
}

function callList(unit) {
  if (!unit.contacts?.length) return null;
  return el("details", { class: "card-more" },
    el("summary", {}, `Whole call list (${unit.contacts.length})`),
    el("ol", {}, unit.contacts.map((c) => el("li", {},
      el("a", { href: `tel:${c.phone}` }, c.name), el("span", { class: "muted" }, ` ${c.role}, ${c.phone}`)))));
}

function planDetails(unit) {
  const plan = unit.plan ?? {};
  if (!Object.values(plan).some(Boolean)) return null;
  const place = [plan.place_name, plan.place_address].filter(Boolean).join(", ");
  return el("details", { class: "card-more", open: unit.backup_low || undefined },
    el("summary", {}, "Emergency plan"),
    plan.supplies ? el("p", {}, el("strong", {}, "Supplies: "), plan.supplies) : null,
    place ? el("p", {}, el("strong", {}, "Go to: "), place, plan.place_phone ? " " : null,
      plan.place_phone ? el("a", { href: `tel:${plan.place_phone}` }, plan.place_phone) : null) : null,
    plan.steps ? el("p", { class: "plan-steps" }, plan.steps) : null);
}

function directions(unit) {
  return el("a", { class: "btn btn-quiet", href: `https://www.google.com/maps/dir/?api=1&destination=${unit.lat},${unit.lon}`,
    target: "_blank", rel: "noopener noreferrer" }, "Directions");
}

// ---- cards ---------------------------------------------------------------------------

export function needsAction(unit) {
  return unit.status === "outage" || unit.status === "offline";
}

export function actionCard(unit, { respondButton, onLocate }) {
  const state = stateOf(unit);
  const head = el("div", { class: "unit-head" },
    el("span", { class: "state-dot", "aria-hidden": "true" }),
    el("h2", {}, el("button", { class: "linklike", type: "button", onclick: () => onLocate(unit), title: "Show on the map" }, unit.name)),
    el("span", { class: "unit-state" }, cardLabel(unit)));
  const facts = el("p", { class: "muted" }, `${unit.equipment}, acuity ${unit.acuity}. ${unit.address}`);

  if (unit.status === "offline") {
    return el("article", { class: `unit action-card is-${state}`, "data-device": unit.device_id }, head, facts,
      el("p", {}, "No report for ", liveTime("since", unit.last_seen),
        ". The unit's Wi-Fi or battery may be down, or the power may be out. Check on the patient."),
      el("div", { class: "card-actions" }, ...callTargets(unit).map((c) => callButton(c)), directions(unit)),
      callList(unit));
  }

  const actions = el("div", { class: "card-actions" },
    unit.critical ? call911() : null,
    unit.acknowledged_by ? null : respondButton(unit),
    ...callTargets(unit).map((c) => callButton(c)),
    directions(unit));
  return el("article", { class: `unit action-card is-${state}`, "data-device": unit.device_id },
    head, facts,
    unit.emergency === "help" ? null : backupClock(unit),
    timeline(unit),
    unit.critical ? el("p", { class: "escalation-note is-critical" }, el("span", { class: "tag tag-out" }, "Under 15 minutes"),
      " County emergency management and care staff have been told. If nobody is with the patient now, call 911.") : null,
    ...escalationNotes(unit),
    actions,
    planDetails(unit),
    callList(unit),
    ...unit.hazards.map((h) => el("p", { class: "unit-hazard" }, el("span", { class: "tag tag-storm" }, "Storm watch"), ` ${h.headline}`)));
}

function cardLabel(unit) {
  if (unit.status === "offline") return "Unit silent";
  if (unit.critical) return "Under 15 min";
  if (unit.unreached) return "No answer";
  if (unit.acknowledged_by) return "Someone responding";
  return WHAT[unit.emergency] ?? "Power out";
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
}

// ---- situation at a glance -------------------------------------------------------------

const SITUATION = [
  { test: (u) => u.status === "outage" && u.critical, label: (n) => `${n} under 15 min: call 911 if nobody is there`, tone: "alarm" },
  { test: (u) => u.status === "outage" && u.unreached, label: (n) => `${n} nobody answering`, tone: "alarm" },
  { test: (u) => u.status === "outage" && u.backup_low, label: (n) => `${n} backup almost out`, tone: "alarm" },
  { test: (u) => u.status === "outage" && !u.acknowledged_by && !u.unreached, label: (n) => `${n} waiting for a responder`, tone: "alarm" },
  { test: (u) => u.status === "outage" && u.acknowledged_by, label: (n) => `${n} someone responding`, tone: "handled" },
  { test: (u) => u.status === "offline", label: (n) => `${n} unit${n === 1 ? "" : "s"} silent`, tone: "quiet" },
  { test: (u) => u.status !== "outage" && u.hazards.length, label: (n) => `${n} in the storm's path`, tone: "storm" },
];

/** One chip per kind of situation with a count; a tap jumps to the first home it counts. */
export function situationChips(units, onPick) {
  const chips = SITUATION.map((kind) => ({ kind, matches: units.filter(kind.test) })).filter((c) => c.matches.length);
  if (!chips.length) return null;
  return el("div", { class: "situation", role: "list" }, chips.map(({ kind, matches }) =>
    el("button", { class: `chip is-${kind.tone}`, type: "button", role: "listitem", onclick: () => onPick(matches[0]) },
      kind.label(matches.length))));
}

// ---- triage mode: many homes at once -------------------------------------------------

export const TRIAGE_AT = 4;  // from this many homes needing someone, cards fold into a ranked list

const URGENCY = (u) => (u.status === "offline" ? 3 : u.critical ? -1 : u.acknowledged_by ? 2 : u.unreached ? 0 : 1);

/** Homes that need someone, most urgent first: nobody answering, then waiting, then someone
    responding, then silent units; within each, the least backup left first. */
export function triageOrder(units) {
  const left = (u) => (u.status === "outage" && u.backup_minutes ? Date.parse(u.outage_started) + u.backup_minutes * 60000 : Infinity);
  return units.filter(needsAction).sort((a, b) => URGENCY(a) - URGENCY(b) || left(a) - left(b));
}

function rowStatus(unit) {
  if (unit.status === "offline") return ["Unit silent", "quiet"];
  if (unit.critical) return ["Under 15 min", "out"];
  if (unit.acknowledged_by) return [`Claimed by ${claimedBy(unit)}`, "acked"];
  if (unit.backup_low) return ["Backup almost out", "out"];
  if (unit.unreached) return ["No answer", "out"];
  return ["Waiting", "out"];
}

function rowClock(unit) {
  if (unit.status === "offline") return el("span", { class: "row-clock is-quiet" }, "silent ", liveTime("since", unit.last_seen));
  if (!unit.backup_minutes || unit.emergency === "help") return el("span", { class: "row-clock" }, unit.emergency === "help" ? "help" : "no backup");
  const runsOut = new Date(Date.parse(unit.outage_started) + unit.backup_minutes * 60000).toISOString();
  const urgent = Date.parse(runsOut) - Date.now() < URGENT_MINUTES * 60000;
  return el("span", { class: `row-clock${urgent ? " is-urgent" : ""}` }, liveTime("until", runsOut));
}

/** One line per home: rank, who and where, time left, status, and the single most useful action.
    Tapping the line opens the full card underneath. */
export function triageRow(unit, rank, { respondButton, canRespond, open, onToggle, onLocate }) {
  const [status, tone] = rowStatus(unit);
  const town = townOf(unit);
  const primary = unit.critical ? call911()
    : canRespond && !unit.acknowledged_by && unit.status === "outage" ? respondButton(unit)
    : callTargets(unit)[0] ? callButton(callTargets(unit)[0], `Call ${shortName(callTargets(unit)[0])}`) : null;
  const line = el("div", { class: "triage-line" },
    el("span", { class: `rank-badge is-${stateOf(unit)}` }, String(rank)),
    el("button", { class: "triage-who", type: "button", "aria-expanded": String(open), onclick: () => onToggle(unit) },
      el("strong", {}, unit.name), el("span", { class: "triage-chevron", "aria-hidden": "true" }, open ? "▾" : "▸")),
    rowClock(unit),
    el("p", { class: "triage-meta" }, el("span", { class: `triage-status is-${tone}` }, status), ` ${town} · ${unit.equipment}`),
    primary);
  const card = open ? actionCard(unit, { respondButton, onLocate }) : null;
  card?.classList.add("is-expanded");
  return el("article", { class: `unit triage-row is-${stateOf(unit)}${open ? " is-open" : ""}`, "data-device": unit.device_id }, line, card);
}

// ---- dispatch tools for triage mode -------------------------------------------------

// One status per home for filtering, in the same words the rows use.
const STATUS_FILTERS = [
  ["critical", "Under 15 min"], ["unreached", "No answer"], ["waiting", "Waiting for a responder"],
  ["claimed", "Claimed"], ["silent", "Unit silent"],
];
export function statusOf(unit) {
  if (unit.status === "offline") return "silent";
  if (unit.critical) return "critical";
  if (unit.acknowledged_by) return "claimed";
  return unit.unreached ? "unreached" : "waiting";
}

export const TRIAGE_FIELDS = [
  { key: "town", label: "Town", value: townOf },
  { key: "status", label: "Status", value: statusOf, names: Object.fromEntries(STATUS_FILTERS), order: STATUS_FILTERS.map(([key]) => key) },
  { key: "equipment", label: "Equipment", value: (u) => u.equipment },
];

/** The shared filter bar for the triage list, then the county welfare-check request. */
export function triageTools(active, { state, onChange, onReset, canSend, onSendCounty, sendNote }) {
  const unreached = active.filter((u) => u.unreached && !u.acknowledged_by).length;
  return [
    ...filterBar(active, TRIAGE_FIELDS, state, { onChange, onReset, idPrefix: "triage", noun: "homes" }),
    canSend && unreached ? onSendCounty(unreached) : null,
    sendNote ? el("p", { class: "send-note", role: "status" }, sendNote) : null,
  ];
}
