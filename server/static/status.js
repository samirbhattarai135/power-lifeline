import { api, el, clockTime, listNames, minutesSince, plural, stateOf, withButton } from "./common.js";

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
      el("div", { class: "verdict-line" }, el("span", { class: "state-dot", "aria-hidden": "true" }), el("h2", {}, VERDICT[state])),
      ...details,
    ),
    whoKnows(unit),
    state === "out" ? respondButton() : null,
  ].filter(Boolean));
  section.className = `is-${state}`;
  section.hidden = false;
  loading.hidden = true;
  document.title = `Lifeline: ${VERDICT[state]}`;
  const announcement = document.getElementById("announce");
  if (announcement.textContent !== VERDICT[state]) announcement.textContent = VERDICT[state];
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
