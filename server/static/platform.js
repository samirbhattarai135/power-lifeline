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
guarded(api("GET", "/api/me")).then(({ email }) => { document.getElementById("who").textContent = email; });

// ---- views ---------------------------------------------------------------------

const views = Object.fromEntries(["board", "patients", "history", "units"]
  .map((name) => [name, document.getElementById(`view-${name}`)]));

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
  else map.invalidateSize();
}
window.addEventListener("hashchange", showView);

// ---- live board ----------------------------------------------------------------

// Zoomed out enough to see weather coming, not just the streets around each home.
const REGIONAL_ZOOM = 9;
const map = L.map("map", { zoomControl: true }).setView(MAP_CENTER, REGIONAL_ZOOM);
let mapFitted = false;
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  { attribution: "© OpenStreetMap contributors", className: "basemap-tiles" }).addTo(map);
const markers = new Map();
const tempLabels = new Map();
const tokens = getComputedStyle(document.documentElement);
const markerColor = Object.fromEntries(
  [["out", "--color-out"], ["acked", "--color-acked"], ["on", "--color-on"], ["quiet", "--color-quiet"]]
    .map(([state, token]) => [state, tokens.getPropertyValue(token).trim()]));

function headline(units) {
  const out = units.filter((u) => u.status === "outage");
  const waiting = out.filter((u) => !u.acknowledged_by);
  const node = document.getElementById("headline");
  node.classList.toggle("is-alarm", waiting.length > 0);
  if (!units.length) {
    node.replaceChildren(el("h1", {}, "No patients yet"), el("p", {}, "Add a patient to start monitoring their power."));
  } else if (!out.length) {
    node.replaceChildren(el("h1", {}, `All ${plural(units.length, "home")} have power`),
      el("p", {}, "We'll alert the call list the moment that changes."));
  } else {
    const responding = out.length - waiting.length;
    node.replaceChildren(el("h1", {}, `${plural(out.length, "home")} without power`),
      el("p", {}, waiting.length ? `${waiting.length} waiting for someone to respond. ${responding ? `${responding} being handled.` : ""}`
                                 : "Someone is responding to each one."));
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
      el("strong", {}, hazardText(hazard)), `: ${listNames(names)}`))),
    el("p", { class: "muted" }, "High-risk warnings have been sent to each patient's first contact. Check backups now, most critical first."),
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
      el("span", { class: "unit-state" }, STATE_LABEL[state])),
    el("p", { class: "muted" }, `${unit.equipment}, acuity ${unit.acuity}. ${unit.address}`),
  ];
  if (unit.status === "outage") {
    lines.push(
      el("p", { class: "unit-backup" }, el("strong", {}, String(unit.backup_minutes_left)), "minutes of backup left"),
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
  for (const hazard of unit.hazards) {
    lines.push(el("p", { class: "unit-hazard" }, el("span", { class: "tag tag-storm" }, "Storm watch"), ` ${hazardText(hazard)}`));
  }
  return el("article", { class: `unit is-${state}` }, lines);
}

function respondButton(unit) {
  const button = el("button", { class: "btn btn-respond btn-small", type: "button" }, "We're responding");
  button.addEventListener("click", () =>
    withButton(button, () => guarded(api("POST", `/api/ack/${encodeURIComponent(unit.device_id)}`, {}))).then(refreshBoard));
  return button;
}

function placeMarker(unit) {
  const state = stateOf(unit);
  const style = { radius: state === "out" ? 13 : 8, color: markerColor[state], fillColor: markerColor[state], fillOpacity: 0.85, weight: 2 };
  let marker = markers.get(unit.device_id);
  if (!marker) {
    marker = L.circleMarker([unit.lat, unit.lon], style).addTo(map);
    markers.set(unit.device_id, marker);
  }
  // A DOM node, not an HTML string: patient names are typed by staff.
  const tip = el("div", { class: "map-tip" }, el("strong", {}, unit.name), el("div", {}, STATE_LABEL[state]),
    unit.conditions ? el("div", {}, conditionsText(unit.conditions)) : null,
    ...unit.hazards.map((h) => el("div", { class: "map-tip-hazard" }, hazardText(h))));
  marker.setLatLng([unit.lat, unit.lon]).setStyle(style).setRadius(style.radius).bindTooltip(tip);
}

// ---- weather on the map ------------------------------------------------------------

const WEATHER_REFRESH_MS = 60000;
const RADAR_REFRESH_MS = 5 * 60000;
// Colours close to the National Weather Service hazard map, so they read like any weather map.
const WARNING_COLORS = {
  "Tornado Warning": "#ff0000", "Severe Thunderstorm Warning": "#ffa500", "Extreme Wind Warning": "#ff8c00",
  "High Wind Warning": "#daa520", "Ice Storm Warning": "#8b008b", "Winter Storm Warning": "#ff69b4",
  "Blizzard Warning": "#ff4500", "Hurricane Warning": "#dc143c", "Tropical Storm Warning": "#b22222",
  "Extreme Cold Warning": "#0000ff", "Extreme Heat Warning": "#c71585", "Excessive Heat Warning": "#c71585",
  "Tornado Watch": "#c9b400", "Severe Thunderstorm Watch": "#db7093", "Hurricane Watch": "#ff00ff",
  "Tropical Storm Watch": "#f08080", "Winter Storm Watch": "#4682b4", "High Wind Watch": "#b8860b",
  "Wind Advisory": "#c19a6b", "Winter Weather Advisory": "#7b68ee", "Extreme Cold Watch": "#5f9ea0",
};

function warningColor(event) {
  return WARNING_COLORS[event.replace(/ \(demo\)$/, "")] ?? "#d97706";
}

const radar = L.tileLayer.wms("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows", {
  layers: "conus_bref_qcd", format: "image/png", transparent: true, opacity: 0.6,
  attribution: "Weather: NOAA NWS",
}).addTo(map);
setInterval(() => radar.setParams({ refreshed: Date.now() }), RADAR_REFRESH_MS);

const warningLayer = L.geoJSON(null, {
  style: (feature) => {
    const high = feature.properties.level === "high";
    return { color: warningColor(feature.properties.event), weight: high ? 3 : 2, dashArray: high ? null : "6 5",
             fillOpacity: high ? 0.16 : 0.07 };
  },
  onEachFeature: (feature, layer) => layer.bindTooltip(
    el("div", { class: "map-tip" }, el("strong", {}, feature.properties.event), el("div", {}, `Until ${dateTime(feature.properties.ends)}`)),
    { sticky: true }),
}).addTo(map);

L.control.layers(null, { "Weather radar": radar, "Warnings and watches": warningLayer },
  { position: "topright", collapsed: matchMedia("(max-width: 640px)").matches }).addTo(map);

const legend = L.control({ position: "bottomleft" });
legend.onAdd = () => el("div", { class: "map-legend", id: "map-legend" });
legend.addTo(map);

function conditionsText(c) {
  const parts = [];
  if (c.temperature_f !== null) parts.push(`${c.temperature_f}°F`);
  if (c.wind_mph !== null) parts.push(`wind ${c.wind_mph} mph${c.gust_mph ? `, gusts ${c.gust_mph}` : ""}`);
  if (c.thunder_percent) parts.push(`${c.thunder_percent}% chance of thunder`);
  return parts.join(", ");
}

const LABEL_MIN_GAP_PX = 44;

// One temperature label per spot on screen: homes close together share the first label,
// and every home's full conditions stay in its hover tooltip.
function placeTemperatures(units) {
  for (const label of tempLabels.values()) label.remove();
  tempLabels.clear();
  const placed = [];
  for (const unit of units) {
    const temperature = unit.conditions?.temperature_f;
    if (temperature === null || temperature === undefined) continue;
    const point = map.latLngToContainerPoint([unit.lat, unit.lon]);
    if (placed.some((other) => other.distanceTo(point) < LABEL_MIN_GAP_PX)) continue;
    placed.push(point);
    const icon = L.divIcon({ className: "temp-label", html: `${Number(temperature)}°`, iconSize: null, iconAnchor: [-12, 9] });
    tempLabels.set(unit.device_id, L.marker([unit.lat, unit.lon], { icon, interactive: false, keyboard: false }).addTo(map));
  }
}
map.on("zoomend", () => placeTemperatures(lastUnits));

function renderLegend(weather) {
  const events = [...new Map(weather.areas.features.map((f) => [f.properties.event, f.properties.level])).entries()];
  const node = document.getElementById("map-legend");
  const swatch = (color, dashed) => el("span", { class: `swatch${dashed ? " is-dashed" : ""}`, style: `--swatch: ${color}` });
  node.replaceChildren(
    el("strong", {}, weather.mode === "demo" ? "Weather (demo storm)" : "Weather"),
    el("div", { class: "legend-row legend-extra" }, el("span", { class: "radar-scale", "aria-hidden": "true" }), "Radar: light to heavy"),
    ...(weather.mode === "off" ? [el("div", { class: "muted" }, "Storm Watch is off")]
      : events.length ? events.map(([event, level]) => el("div", { class: "legend-row" }, swatch(warningColor(event), level !== "high"), event))
      : [el("div", { class: "muted" }, "No warnings or watches near your homes")]),
    weather.checked_at ? el("div", { class: "muted legend-extra" }, `Checked ${secondsAgo(weather.checked_at)}`) : null,
  );
}

async function refreshWeather() {
  try {
    const weather = await guarded(api("GET", "/api/weather-map"));
    warningLayer.clearLayers();
    warningLayer.addData(weather.areas);
    renderLegend(weather);
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      document.getElementById("map-legend").replaceChildren(el("strong", {}, "Weather"), el("div", { class: "muted" }, "Weather unavailable, retrying"));
    }
  }
}

function dropStaleMarkers(units) {
  const live = new Set(units.map((u) => u.device_id));
  for (const [id, marker] of markers) if (!live.has(id)) { marker.remove(); markers.delete(id); }
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
    if (currentView() === "units") renderUnits();
    headline(units);
    stormBanner(units);
    announce(document.querySelector("#headline h1").textContent);
    document.getElementById("units").replaceChildren(...units.map(unitRow));
    units.forEach(placeMarker);
    dropStaleMarkers(units);
    placeTemperatures(units);
    if (!mapFitted && units.length) {
      // Once, on first load: every home in view with room around it, then the user is in charge.
      map.fitBounds(L.latLngBounds(units.map((u) => [u.lat, u.lon])).pad(1.5), { maxZoom: REGIONAL_ZOOM });
      mapFitted = true;
    }
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
      el("p", { class: "muted" }, "Add the first patient, then put their unit ID and token on the power unit."),
      el("button", { class: "btn", type: "button", onclick: () => openForm() }, "Add patient")));
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
      el("td", {}, el("button", { class: "btn btn-quiet btn-small", type: "button", onclick: () => openForm(p) }, "Edit")),
    ))),
  ));
}

document.getElementById("add-patient").addEventListener("click", () => openForm());

// ---- history ---------------------------------------------------------------------

const historyDays = document.getElementById("history-days");
historyDays.addEventListener("change", loadHistory);
document.getElementById("history-print").addEventListener("click", () => window.print());

const KIND_LABEL = { power: "Power outage", silent: "Unit silent", storm: "Storm notice" };
const KIND_TAG = { power: "tag-out", silent: "", storm: "tag-storm" };

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

function historySummary(incidents, period) {
  const outages = incidents.filter((i) => i.kind === "power");
  const silent = incidents.filter((i) => i.kind === "silent").length;
  const storms = incidents.filter((i) => i.kind === "storm").length;
  const silentNote = (silent ? ` ${plural(silent, "unit")} went silent.` : "") +
    (storms ? ` ${plural(storms, "storm notice")} sent ahead of bad weather.` : "");
  if (!outages.length) return `No power outages in the ${period}.${silentNote}`;
  const answered = outages.filter((i) => i.response_minutes !== null).map((i) => i.response_minutes).sort((a, b) => a - b);
  const median = answered.length ? answered[Math.floor((answered.length - 1) / 2)] : null;
  const response = answered.length
    ? `Someone responded to ${answered.length} of ${outages.length}, with a typical response time of ${durationText(median)}.`
    : "None acknowledged yet.";
  return `${plural(outages.length, "power outage")} in the ${period}. ${response}${silentNote}`;
}

function historyRow(incident) {
  const ongoing = incident.ended_at === null;
  return el("tr", {},
    el("td", { class: "num", "data-label": "Started" }, dateTime(incident.started_at)),
    el("td", { "data-label": "Patient" }, el("strong", {}, incident.patient_name), el("div", { class: "muted code" }, incident.device_id)),
    el("td", { "data-label": "What happened" },
      el("span", { class: `tag ${KIND_TAG[incident.kind]}` }, KIND_LABEL[incident.kind]),
      el("div", { class: "muted" }, incident.kind === "storm" ? incident.detail : incident.equipment)),
    el("td", { class: "num", "data-label": "Lasted" },
      ongoing ? el("span", { class: "tag tag-ongoing" }, "Ongoing") : durationText(incident.duration_minutes)),
    el("td", { class: "cell-wide", "data-label": "Alerted" },
      incident.alerted.length ? listNames(incident.alerted.map((a) => a.name)) : "Nobody yet"),
    el("td", { class: "cell-wide", "data-label": "Response" }, responseText(incident)));
}

function responseText(incident) {
  if (incident.kind !== "power") return el("span", { class: "muted" }, "Not needed");
  if (incident.acknowledged_by) return `${incident.acknowledged_by} after ${durationText(incident.response_minutes)}`;
  if (incident.ended_at === null) return "Waiting";
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

function renderUnits() {
  const body = document.getElementById("units-body");
  if (!lastUnits.length) {
    body.replaceChildren(el("div", { class: "empty" }, el("h2", {}, "No units yet"),
      el("p", { class: "muted" }, "Add a patient and set up their power unit to see it here.")));
    return;
  }
  const units = [...lastUnits].sort((a, b) => a.name.localeCompare(b.name));
  body.replaceChildren(el("table", { class: "stack-table" },
    el("thead", {}, el("tr", {}, ["Patient", "Status", "Last report", "Wi-Fi", "Firmware", "Running for"].map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, units.map((unit) => el("tr", {},
      el("td", { "data-label": "Patient" }, el("strong", {}, unit.name), el("div", { class: "muted code" }, unit.device_id)),
      el("td", { "data-label": "Status" }, el("span", { class: `tag ${stateOf(unit) === "out" ? "tag-out" : ""}` }, STATE_LABEL[stateOf(unit)])),
      el("td", { class: "num", "data-label": "Last report" }, unit.last_seen ? secondsAgo(unit.last_seen) : "Never"),
      el("td", { class: "cell-wide", "data-label": "Wi-Fi" }, signalCell(unit.rssi)),
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
    el("div", { class: "grid-2" },
      field("Latitude", input("lat", patient?.lat, { inputmode: "decimal", required: true })),
      field("Longitude", input("lon", patient?.lon, { inputmode: "decimal", required: true }))),
    el("p", { class: "hint muted" }, "Right-click the home in Google Maps to copy its latitude and longitude."),
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

showView();
refreshBoard();
setInterval(refreshBoard, BOARD_REFRESH_MS);
refreshWeather();
setInterval(refreshWeather, WEATHER_REFRESH_MS);
