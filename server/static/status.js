// The family status page (/s/<token>): one patient, what is happening, what to do, and the 911 card.
import { api, el, clockTime, durationText, hazardText, listNames, minutesSince, nameParts, plural, runsOutAt, withButton } from "./common.js";
import { clockText, icon, liveTime, startClock, tickAll } from "./actions.js";

const shareToken = location.pathname.split("/").pop();
const section = document.getElementById("status");
const loading = document.getElementById("loading");
const updated = document.getElementById("updated");
const REFRESH_MS = 5000;

const SVG = "http://www.w3.org/2000/svg";
const PATHS = {
  powerOff: "M10.5 3.5 9 7M13 2l-1.5 6H17l-2.5 4.2M8.4 11H5l6 11 1.6-6M2 2l20 20",
  alarm: "M7 18v-6a5 5 0 1 1 10 0v6M5 21a1 1 0 0 1-1-1v-1a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v1a1 1 0 0 1-1 1zM21 12h1M18.5 4.5 18 5M2 12h1M12 2v1M4.9 4.9l.7.7M12 12v6",
  cloud: "M4 14.9A7 7 0 1 1 15.7 8h1.8a4.5 4.5 0 0 1 2.5 8.2M8 15h.01M8 19h.01M12 17h.01M12 21h.01M16 15h.01M16 19h.01",
};
function bigIcon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", PATHS[name]);
  svg.append(path);
  return svg;
}

const shortName = (unit) => nameParts(unit.name)[0];
const firstWord = (unit) => shortName(unit).split(" ")[0];

function verdictFor(unit) {
  if (unit.status === "outage" && unit.emergency === "machine") return `The ${unit.equipment.toLowerCase()} has stopped`;
  if (unit.status === "outage" && unit.emergency === "help") return "Help was requested";
  if (unit.status === "outage") return "Power is out";
  return unit.status === "ok" ? "Power is on" : "No signal from the unit";
}

const tone = (unit) => (unit.status === "outage" ? "out" : unit.status === "ok" ? "on" : "quiet");

function agoNode(iso) {
  return el("span", { class: "ago", "data-ago": iso }, agoText(iso));
}
function agoText(iso) {
  const minutes = minutesSince(iso);
  return minutes ? `${durationText(minutes)} ago` : "just now";
}

// ---- the status card -----------------------------------------------------------------

function statusCard(unit) {
  const head = el("div", { class: "status-band" },
    el("h2", {}, unit.status === "outage" ? bigIcon("powerOff") : null, verdictFor(unit)),
    unit.status === "outage" ? el("p", {}, `Out since ${clockTime(unit.outage_started)}, `, agoNode(unit.outage_started)) : null);
  let body;
  if (unit.status === "outage" && unit.backup_minutes && unit.emergency !== "help") {
    const runsOut = runsOutAt(unit);
    body = el("div", { class: "status-body backup-clock", "data-total": String(unit.backup_minutes * 60000) },
      el("p", { class: "muted" }, `${unit.equipment} backup`),
      el("p", { class: "family-backup" }, el("span", { class: "backup-clock-value" }, liveTime("until", runsOut)), el("span", { class: "left-word" }, " left")),
      el("div", { class: "backup-bar", "aria-hidden": "true" }, el("span")),
      el("p", { class: "backup-clock-note" }, `Runs out around ${clockTime(runsOut)}`));
  } else if (unit.status === "outage") {
    body = el("div", { class: "status-body" }, el("p", {}, unit.emergency === "help" ? "Someone pressed the help button on the unit." : `${unit.equipment} has no battery backup.`));
  } else if (unit.status === "ok") {
    body = el("div", { class: "status-body" }, el("p", {}, "We'll text you right away if that changes."));
  } else if (unit.status === "never_seen") {
    body = el("div", { class: "status-body" }, el("p", {}, "The power unit hasn't connected yet. The care agency will finish setting it up."));
  } else {
    body = el("div", { class: "status-body" }, el("p", {}, `We haven't heard from the power unit for ${plural(minutesSince(unit.last_seen), "minute")}. ` +
      `Its Wi-Fi may be down or its battery may be empty. Please check on ${firstWord(unit)} if you can.`));
  }
  return el("div", { class: `status-card is-${tone(unit)}` }, head, body);
}

// The emergency action on its own card, with nothing else in it.
function card911(unit) {
  return el("div", { class: "card-911" },
    el("p", {}, bigIcon("alarm"), `If ${firstWord(unit)}'s breathing is in danger, call 911 now. Don't wait for this page.`),
    el("a", { class: "btn btn-911 btn-huge", href: "tel:911" }, icon("phone"), "Call 911"));
}

function whoKnows(unit) {
  if (unit.status !== "outage") return null;
  const alerted = unit.contacts_alerted_at?.length ? unit.contacts_alerted_at : unit.contacts_alerted.map((name) => ({ name, at: null }));
  const lead = unit.acknowledged_by
    ? `${unit.acknowledged_by} is responding, so we've stopped alerting the rest of the call list.`
    : alerted.length ? `We've alerted ${listNames(alerted.map((a) => a.name))}. If nobody responds, we'll alert the next person on the list.` : null;
  const steps = [
    ...alerted.map((a) => el("li", { class: "step is-done" }, el("strong", {}, `Alerted ${a.name}`), a.at ? ` ${clockTime(a.at)}` : null)),
    unit.acknowledged_by ? el("li", { class: "step is-claimed" }, el("strong", {}, `${unit.acknowledged_by} responded`), unit.acknowledged_at ? ` at ${clockTime(unit.acknowledged_at)}` : null)
      : unit.next_contact && unit.next_alert_at ? el("li", { class: "step is-next" }, el("strong", {}, `Next: ${unit.next_contact.name}`), " in ", liveTime("until", unit.next_alert_at), " if nobody responds") : null,
  ].filter(Boolean);
  return el("div", { class: "who-knows" }, lead ? el("p", {}, lead) : null, steps.length ? el("ol", { class: "incident-steps" }, steps) : null);
}

function respondButton() {
  const button = el("button", { class: "btn btn-respond btn-huge", type: "button" }, icon("hand"), "I'm responding");
  button.addEventListener("click", () =>
    withButton(button, () => api("POST", `/api/family/${shareToken}/ack`, {})).then(refresh).catch(showError));
  return button;
}

function stormNote(unit) {
  if (!unit.hazards.length) return null;
  const advice = unit.status === "outage" ? null : "Charge backup batteries and check supplies now. If the power goes out, we'll alert the call list right away.";
  return el("div", { class: "storm-note" }, bigIcon("cloud"),
    el("div", {}, el("h2", {}, "Storm watch"), unit.hazards.map((hazard) => el("p", {}, `${hazardText(hazard)}.`)), advice ? el("p", {}, advice) : null));
}

// The agency's plan: open when it matters, folded away otherwise.
function planSection(unit) {
  const plan = unit.plan;
  if (!plan) return null;
  const urgent = unit.status === "outage" || unit.hazards.length > 0;
  const parts = [];
  if (plan.supplies) parts.push(el("div", { class: "plan-part" }, el("h3", {}, "Backup supplies"), el("p", { class: "plan-text" }, plan.supplies)));
  if (plan.place_name || plan.place_address) {
    parts.push(el("div", { class: "plan-part" }, el("h3", {}, "Nearest place with power"),
      el("p", {}, el("strong", {}, plan.place_name)),
      plan.place_address ? el("p", {}, plan.place_address) : null,
      plan.place_phone ? el("p", {}, el("a", { href: `tel:${plan.place_phone.replace(/[^+\d]/g, "")}` }, plan.place_phone)) : null));
  }
  if (plan.steps) parts.push(el("div", { class: "plan-part" }, el("h3", {}, "What to do"), el("p", { class: "plan-text" }, plan.steps)));
  return el("details", { class: "plan", open: urgent || undefined }, el("summary", {}, "Emergency plan"), ...parts);
}

// ---- the tab title carries the status, so a glance at the tab is enough -------------------

let current = null;

function titleText(unit) {
  const who = shortName(unit);
  if (unit.status === "outage") {
    const runsOut = runsOutAt(unit);
    const left = runsOut ? Date.parse(runsOut) - Date.now() : null;
    const backup = left === null ? "" : left > 0 ? ` · ${clockText(left)} left` : " · backup may be out";
    return `${unit.emergency === "power" || !unit.emergency ? "Power out" : verdictFor(unit)}${backup} · ${who}`;
  }
  return `${unit.status === "ok" ? "Power on" : "No signal"} · ${who}`;
}

function tickPage() {
  for (const node of section.querySelectorAll("[data-ago]")) node.textContent = agoText(node.dataset.ago);
  if (current) document.title = titleText(current);
}

// ---- render ----------------------------------------------------------------------------

const VOLATILE = new Set(["backup_minutes_left", "last_seen"]);
let lastStatus = "";

function render(unit) {
  current = unit;
  updated.textContent = `Checked at ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}. This page refreshes on its own.`;
  updated.classList.remove("is-stale");
  const snapshot = JSON.stringify(unit, (key, value) => (VOLATILE.has(key) && unit.status !== "offline" ? undefined : value));
  tickPage();
  if (snapshot === lastStatus) return;  // keep the button stable between polls
  lastStatus = snapshot;
  const urgent = unit.status === "outage" || unit.status === "offline";
  section.replaceChildren(...[
    el("h1", {}, `${shortName(unit)}'s home`),
    statusCard(unit),
    urgent ? card911(unit) : null,
    whoKnows(unit),
    unit.status === "outage" && !unit.acknowledged_by ? respondButton() : null,
    stormNote(unit),
    planSection(unit),
  ].filter(Boolean));
  document.getElementById("foot-911").hidden = urgent;
  section.hidden = false;
  loading.hidden = true;
  tickAll(section);
  const announcement = document.getElementById("announce");
  if (announcement.textContent !== verdictFor(unit)) announcement.textContent = verdictFor(unit);
}

let lastGood = null;
function showError(error) {
  lastStatus = "";
  if (error.status === 404) {
    loading.hidden = false;
    loading.textContent = error.message;
    section.hidden = true;
    return;
  }
  // Keep what we last knew on screen, and say plainly that it is not current.
  updated.classList.add("is-stale");
  updated.textContent = lastGood ? `Not updating since ${clockTime(lastGood.toISOString())}. Retrying…` : "Can't reach Lifeline right now. Retrying…";
  if (section.hidden) { loading.hidden = false; loading.textContent = "Can't reach Lifeline right now. Retrying…"; }
}

async function refresh() {
  try {
    const unit = await api("GET", `/api/family/${shareToken}`);
    lastGood = new Date();
    render(unit);
  } catch (error) {
    showError(error);
  }
}

startClock(section);
setInterval(tickPage, 15000);
refresh();
setInterval(refresh, REFRESH_MS);
