import { api, el, clockTime, hazardText, listNames, minutesSince, plural, stateOf, withButton } from "./common.js";

const shareToken = location.pathname.split("/").pop();
const section = document.getElementById("status");
const loading = document.getElementById("loading");
const updated = document.getElementById("updated");
const REFRESH_MS = 5000;

const VERDICT = {
  out: "Power is out",
  acked: "Power is out",
  on: "Power is on",
  quiet: "No signal from the unit",
};

function verdictFor(unit, state) {
  if (unit.status === "outage" && unit.emergency === "machine") return `The ${unit.equipment.toLowerCase()} has stopped`;
  if (unit.status === "outage" && unit.emergency === "help") return "Help was requested";
  return VERDICT[state];
}

function firstName(fullName) {
  return fullName.replace(/\s*\(.*\)$/, "");
}

function outageDetails(unit) {
  const minutes = minutesSince(unit.outage_started);
  const since = `Out since ${clockTime(unit.outage_started)}, ${minutes ? `${plural(minutes, "minute")} ago` : "just now"}.`;
  const left = unit.backup_minutes_left;
  const share = unit.backup_minutes ? Math.min(1, left / unit.backup_minutes) : 0;
  return [
    el("p", {}, since),
    el("div", { class: "backup" },
      el("p", {}, `${unit.equipment} backup`),
      el("p", { class: "backup-number" }, left > 0 ? `About ${plural(left, "minute")} left` : "Backup may be running out"),
      meter(share),
    ),
  ];
}

function meter(share) {
  const fill = el("span");
  fill.style.transform = `scaleX(${share})`;
  return el("div", { class: "meter", role: "img", "aria-label": `${Math.round(share * 100)} percent of backup left` }, fill);
}

function quietDetails(unit) {
  if (unit.status === "never_seen") {
    return [el("p", {}, "The power unit hasn't connected yet. The care agency will finish setting it up.")];
  }
  return [el("p", {}, `We haven't heard from the power unit for ${plural(minutesSince(unit.last_seen), "minute")}. ` +
    "Its Wi-Fi may be down or its battery may be empty. Please check on " + firstName(unit.name) + " if you can.")];
}

function whoKnows(unit) {
  if (unit.acknowledged_by) {
    return el("p", {}, `${unit.acknowledged_by} is responding, so we've stopped alerting the rest of the call list.`);
  }
  if (unit.contacts_alerted.length) {
    return el("p", {}, `We've alerted ${listNames(unit.contacts_alerted)}. If nobody responds, we'll alert the next person on the list.`);
  }
  return null;
}

function respondButton() {
  const button = el("button", { class: "btn btn-respond", type: "button" }, "I'm responding");
  button.addEventListener("click", () =>
    withButton(button, () => api("POST", `/api/family/${shareToken}/ack`, {})).then(refresh).catch(showError));
  return button;
}

// The agency's plan: front and centre when it matters, folded away otherwise.
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
  if (urgent) return el("section", { class: "plan is-open" }, el("h2", {}, "Your emergency plan"), ...parts);
  return el("details", { class: "plan" }, el("summary", {}, "Your emergency plan"), ...parts);
}

function stormNote(unit) {
  if (!unit.hazards.length) return null;
  const advice = unit.status === "outage" ? "" : " Charge backup batteries and check supplies now. If the power goes out, we'll alert the call list right away.";
  return el("div", { class: "storm-note" },
    el("h2", {}, "Storm watch"),
    unit.hazards.map((hazard) => el("p", {}, el("strong", {}, hazardText(hazard)), ".")),
    advice ? el("p", {}, advice.trim()) : null);
}

let lastStatus = "";

function render(unit) {
  const snapshot = JSON.stringify(unit);
  updated.textContent = `Checked at ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}. This page refreshes on its own.`;
  if (snapshot === lastStatus) return;  // keep the button stable between polls
  lastStatus = snapshot;
  const state = stateOf(unit);
  const details = unit.status === "outage" ? outageDetails(unit)
    : unit.status === "ok" ? [el("p", {}, "We'll text you right away if that changes.")]
    : quietDetails(unit);

  section.replaceChildren(...[
    el("h1", {}, `${firstName(unit.name)}'s home`),
    el("div", { class: `verdict is-${state}` },
      el("div", { class: "verdict-line" }, el("span", { class: "state-dot", "aria-hidden": "true" }), el("h2", {}, verdictFor(unit, state))),
      ...details,
    ),
    whoKnows(unit),
    state === "out" ? respondButton() : null,
    stormNote(unit),
    planSection(unit),
  ].filter(Boolean));
  section.className = `is-${state}`;
  section.hidden = false;
  loading.hidden = true;
  document.title = `Lifeline: ${verdictFor(unit, state)}`;
  const announcement = document.getElementById("announce");
  if (announcement.textContent !== verdictFor(unit, state)) announcement.textContent = verdictFor(unit, state);
}

function showError(error) {
  lastStatus = "";
  loading.hidden = false;
  loading.textContent = error.status === 404 ? error.message : "Can't reach Lifeline right now. Retrying…";
  if (error.status === 404) section.hidden = true;
}

async function refresh() {
  try {
    render(await api("GET", `/api/family/${shareToken}`));
  } catch (error) {
    showError(error);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
