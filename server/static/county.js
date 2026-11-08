// County emergency management: every home without power, most urgent first, read only, no phone numbers.
import { ApiError, api, el, backupTone, chip, clockTime, durationText, glyph, hazardText, homeState, listNames, nameNode, nameParts, plural } from "./common.js";
import { countyContext } from "./escalation.js";
import { applyFilters, filterBar, mountFilterBar, resetState } from "./filters.js";

const REFRESH_MS = 15000;
const EMERGENCY_LABEL = { power: "Power out", machine: "Machine stopped", help: "Help requested" };

// ---- one row per home --------------------------------------------------------------

function statusCell(home) {
  const state = homeState(home);
  const word = home.status === "never_seen" ? "Not set up"
    : state === "out" && home.emergency && home.emergency !== "power" ? EMERGENCY_LABEL[home.emergency] : undefined;
  const detail = home.status === "outage" ? `${EMERGENCY_LABEL[home.emergency] ?? "Power out"} since ${clockTime(home.outage_started)}`
    : home.status === "offline" && home.last_seen ? `Last report ${clockTime(home.last_seen)}`
    : home.status === "ok" ? "" : "No report yet";
  return el("div", { class: "county-status" }, chip(state, word), detail ? el("span", { class: "muted" }, detail) : null);
}

function backupCell(home) {
  if (home.status === "offline" || home.status === "never_seen") return el("span", { class: "county-backup is-unknown" }, "Unknown");
  if (home.status !== "outage") return el("span", { class: "muted" }, "Not in use");
  const left = home.backup_minutes_left;
  if (!left) return el("span", { class: "county-backup tone-red" }, "Out now");
  return el("span", { class: `county-backup tone-${backupTone(left * 60000)}` }, durationText(left));
}

function respondingCell(home) {
  if (home.status !== "outage") return el("span", { class: "muted" }, "—");
  if (home.responder) return `${home.responder}${home.responder_at ? `, ${clockTime(home.responder_at)}` : ""}`;
  return el("strong", { class: home.critical ? "nobody is-critical" : "nobody" }, "Nobody yet");
}

function row(home, rank) {
  const map = `https://www.google.com/maps?q=${home.lat},${home.lon}`;
  return el("tr", { class: `is-${homeState(home)}` },
    el("td", { class: "num county-rank", "data-label": "#" }, rank ? String(rank) : ""),
    el("td", { "data-label": "Patient" }, nameNode(home.name), el("div", { class: "muted" }, `${home.equipment}, acuity ${home.acuity}`)),
    el("td", { class: "cell-wide", "data-label": "Address" }, home.address,
      el("div", {}, el("a", { href: map, target: "_blank", rel: "noopener noreferrer" }, "Open in maps", el("span", { class: "visually-hidden" }, " (opens a new tab)")))),
    el("td", { "data-label": "Status" }, statusCell(home)),
    el("td", { class: "num", "data-label": "Backup left" }, backupCell(home)),
    el("td", { "data-label": "Responding" }, respondingCell(home)));
}

// ---- filters ------------------------------------------------------------------------

const STATUS = { critical: "Under 15 min", unreached: "No answer", waiting: "Power out", responding: "Responding",
                 silent: "Unit silent", on: "Power on", unset: "Not set up" };
function statusKey(home) {
  if (home.status === "outage") return home.responder ? "responding" : home.critical ? "critical" : home.unreached ? "unreached" : "waiting";
  return { offline: "silent", ok: "on", never_seen: "unset" }[home.status];
}
const townOf = (home) => home.address.split(",").slice(-2, -1)[0]?.trim() || "Unknown town";
const FIELDS = [
  { key: "status", label: "Status", value: statusKey, names: STATUS, order: Object.keys(STATUS) },
  { key: "town", label: "Town", value: townOf },
];
const state = { status: null, town: null, search: "" };
const searchText = (home) => `${home.name} ${home.address}`;
let homesNow = [];
let toolsKey = "";
let showPowered = false;  // homes with power stay folded away unless asked for

function renderTools(force = false) {
  const key = JSON.stringify([homesNow.map((h) => [statusKey(h), townOf(h)]), state]);
  if (!force && key === toolsKey) return;
  toolsKey = key;
  mountFilterBar(document.getElementById("county-tools"), filterBar(homesNow, FIELDS, state, {
    onChange: (k, v) => { state[k] = v; render(); },
    onReset: () => { resetState(FIELDS, state); render(); },
    onSearch: (text) => { state.search = text; render(); },
    searchLabel: "Search", searchText, idPrefix: "county", noun: "homes",
  }));
}

function render() {
  renderTools();
  const shown = applyFilters(homesNow, FIELDS, state, searchText);
  const table = document.getElementById("county-table");
  if (!shown.length) {
    table.replaceChildren(el("div", { class: "empty" }, el("h2", {}, "No homes match these filters"), el("p", { class: "muted" }, "Remove a filter above, or reset them.")));
    return;
  }
  // Asking for "Power on" by filter shows them; otherwise they fold behind one line.
  const fold = state.status !== "on";
  const needs = fold ? shown.filter((h) => h.status !== "ok") : shown;
  const powered = fold ? shown.filter((h) => h.status === "ok") : [];
  const toggle = el("button", { class: "county-fold", type: "button", "aria-expanded": String(showPowered),
    onclick: () => { showPowered = !showPowered; render(); } },
    el("span", { class: "fold-chevron", "aria-hidden": "true" }, showPowered ? "▾" : "▸"), glyph("on"),
    `${plural(powered.length, "home")} with power${showPowered ? "" : ", hidden"}`);
  table.replaceChildren(el("table", { class: "stack-table county-table" },
    el("thead", {}, el("tr", {}, ["#", "Patient", "Address", "Status", "Backup left", "Responding"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, needs.map((home, index) => row(home, home.status === "outage" ? index + 1 : null))),
    powered.length ? el("tbody", { class: "county-powered" },
      el("tr", { class: "county-fold-row" }, el("td", { colspan: 6 }, toggle)),
      showPowered ? powered.map((home) => row(home, null)) : []) : null));
}

// ---- the storm, said once ----------------------------------------------------------------

function stormBanner(homes) {
  const byHazard = new Map();
  for (const home of homes) {
    const details = home.hazard_details?.length ? home.hazard_details : home.hazards.map((headline) => ({ headline }));
    for (const hazard of details) {
      const text = hazard.ends ? hazardText(hazard) : hazard.headline;
      byHazard.set(text, (byHazard.get(text) ?? 0) + 1);
    }
  }
  const node = document.getElementById("county-banner");
  if (!byHazard.size) { node.replaceChildren(); return; }
  node.replaceChildren(el("div", { class: "county-storm", role: "region", "aria-label": "Weather warning" },
    el("p", {}, [...byHazard].flatMap(([text, count], index) => [index ? " " : "", el("strong", {}, `${text}.`),
      ` ${plural(count, "home")} on this list ${count === 1 ? "is" : "are"} inside the warning area.`]),
      " Shown once here instead of on every row.")));
}

function counts(out) {
  const waiting = out.filter((h) => !h.responder);
  const items = [
    [out.filter((h) => homeState(h) === "critical").length, "under 15 min of backup", "is-red"],
    [waiting.length, "nobody responding yet", "is-red"],
    [out.length - waiting.length, "someone responding", "is-amber"],
  ];
  document.getElementById("county-counts").replaceChildren(...(out.length
    ? items.map(([n, label, tone]) => el("div", { class: tone }, el("dt", {}, label), el("dd", {}, String(n)))) : []));
}

// A home newly under 15 minutes is announced once, assertively.
let knownCritical = null;
function flagNewlyRed(homes) {
  const critical = homes.filter((h) => homeState(h) === "critical");
  if (knownCritical) {
    const fresh = critical.filter((h) => !knownCritical.has(h.name));
    if (fresh.length) document.getElementById("announce-alert").textContent =
      `Under 15 minutes of backup: ${listNames(fresh.map((h) => nameParts(h.name)[0])).replace(/\.$/, "")}. A welfare check is needed if nobody is there.`;
  }
  knownCritical = new Set(critical.map((h) => h.name));
}

let lastGood = null;

async function refresh() {
  try {
    const { homes, counties } = await api("GET", "/api/county");
    lastGood = new Date();
    const out = homes.filter((h) => h.status === "outage");
    const allPower = out.every((h) => (h.emergency ?? "power") === "power");
    const title = !out.length ? `All ${plural(homes.length, "home")} have power`
      : allPower ? `${plural(out.length, "home")} without power` : `${plural(out.length, "home")} need help now`;
    const heading = document.getElementById("county-title");
    heading.textContent = title;
    heading.classList.toggle("is-alarm", out.length > 0);
    document.title = `Lifeline: ${title}`;
    homesNow = homes;
    counts(out);
    stormBanner(homes);
    render();
    flagNewlyRed(homes);
    const announce = document.getElementById("announce");
    const spoken = [title, ...[...document.querySelectorAll("#county-counts > div")].map((d) => `${d.querySelector("dd").textContent} ${d.querySelector("dt").textContent}`)].join(". ");
    if (announce.textContent !== spoken) announce.textContent = spoken;
    document.getElementById("county-body").replaceChildren(countyContext(counties) ?? "");
    const updated = document.getElementById("updated");
    updated.classList.remove("is-stale");
    updated.textContent = `Updated ${lastGood.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}. Refreshes every 15 seconds.`;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      location.assign("/login");
      return;
    }
    // Keep the last picture, but never let it look live.
    const updated = document.getElementById("updated");
    updated.classList.add("is-stale");
    updated.textContent = lastGood ? `Not updating since ${clockTime(lastGood.toISOString())}. Retrying every 15 seconds.` : "Can't reach Lifeline right now. Retrying.";
  }
}

document.getElementById("signout").addEventListener("click", async () => {
  await api("POST", "/api/logout", {}).catch(() => {});  // offline: still leave the page
  location.assign("/login");
});
const accountToggle = document.getElementById("account-toggle");
accountToggle.addEventListener("click", () => {
  const open = accountToggle.getAttribute("aria-expanded") !== "true";
  accountToggle.setAttribute("aria-expanded", String(open));
  document.getElementById("account-menu").classList.toggle("is-open", open);
});
api("GET", "/api/me").then((me) => { document.getElementById("who").textContent = me.email; document.getElementById("role").textContent = me.role_label; }).catch(() => {});

refresh();
setInterval(refresh, REFRESH_MS);
