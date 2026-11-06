import { ApiError, api, el, clockTime, durationText, listNames, minutesSince, plural } from "./common.js";
import { countyContext, escalationNotes } from "./escalation.js";

const REFRESH_MS = 15000;
const STATUS_TEXT = { outage: "Power out", offline: "Unit silent", ok: "Power on", never_seen: "Not set up" };

const EMERGENCY_LABEL = { power: "Power out", machine: "Machine stopped", help: "Help requested" };

// Same dot language as the agency board: red out, amber someone responding, green on, grey unknown.
function stateClass(home) {
  if (home.status === "outage") return home.responder ? "is-acked" : "is-out";
  return home.status === "ok" ? "is-on" : "is-quiet";
}

function statusLine(home, label, ...detail) {
  return el("div", { class: `county-status ${stateClass(home)}` },
    el("span", { class: "state-dot", "aria-hidden": "true" }), el("div", {}, label, ...detail));
}

function statusCell(home) {
  if (home.status === "outage") {
    return statusLine(home,
      el("span", { class: "tag tag-out" }, EMERGENCY_LABEL[home.emergency] ?? "Power out"),
      el("div", {}, `since ${clockTime(home.outage_started)}, ${durationText(minutesSince(home.outage_started))}`));
  }
  if (home.status === "offline") {
    return statusLine(home, el("span", { class: "tag" }, "Unit silent"),
      el("div", { class: "muted" }, `no report for ${durationText(minutesSince(home.last_seen))}; Wi-Fi or power may be down`));
  }
  return statusLine(home, el("span", { class: "muted" }, STATUS_TEXT[home.status]));
}

function row(home) {
  const backup = home.status === "outage"
    ? el("strong", { class: home.backup_minutes_left < 60 ? "no-response" : "" }, durationText(home.backup_minutes_left))
    : el("span", { class: "muted" }, "Not in use");
  const map = `https://www.google.com/maps?q=${home.lat},${home.lon}`;
  return el("tr", {},
    el("td", { "data-label": "Patient" }, el("strong", {}, home.name), el("div", { class: "muted" }, `${home.equipment}, acuity ${home.acuity}`)),
    el("td", { class: "cell-wide", "data-label": "Address" }, home.address, el("div", {}, el("a", { href: map, target: "_blank", rel: "noopener noreferrer" }, "Open in maps"))),
    el("td", { "data-label": "Status" }, statusCell(home), escalationNotes(home, "county"),
      home.hazards.length ? el("div", {}, el("span", { class: "tag tag-storm" }, "Storm watch"), ` ${home.hazards.join(", ")}`) : null),
    el("td", { class: "num", "data-label": "Backup left" }, backup),
    el("td", { "data-label": "Responding" }, home.status !== "outage" ? el("span", { class: "muted" }, "—")
      : home.responder ?? el("span", { class: "no-response" }, "Nobody yet")));
}

async function refresh() {
  try {
    const { homes, counties } = await api("GET", "/api/county");
    const out = homes.filter((h) => h.status === "outage");
    const waiting = out.filter((h) => !h.responder);
    const allPower = out.every((h) => (h.emergency ?? "power") === "power");
    const title = !out.length ? `All ${plural(homes.length, "home")} are fine`
      : allPower ? `${plural(out.length, "home")} without power` : `${plural(out.length, "home")} need help now`;
    document.getElementById("county-title").textContent = title;
    document.title = `Lifeline: ${title}`;
    const announce = document.getElementById("announce");
    if (announce.textContent !== title) announce.textContent = title;
    document.getElementById("county-body").replaceChildren(
      out.length ? el("p", { class: "summary" }, waiting.length ? `${waiting.length} with nobody responding yet: ${listNames(waiting.map((h) => h.name))}.` : "Someone is responding to each one.") : "",
      countyContext(counties) ?? "",
      el("table", { class: "stack-table" },
        el("thead", {}, el("tr", {}, ["Patient", "Address", "Status", "Backup left", "Responding"].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, homes.map(row))));
    document.getElementById("updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}. Refreshes every 15 seconds.`;
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      location.assign("/login");
      return;
    }
    document.getElementById("county-title").textContent = "Can't reach Lifeline right now";
  }
}

document.getElementById("signout").addEventListener("click", async () => {
  await api("POST", "/api/logout", {});
  location.assign("/login");
});

refresh();
setInterval(refresh, REFRESH_MS);
