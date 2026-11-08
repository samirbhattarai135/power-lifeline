// The live board's map: patient homes as state shapes, radar, warning outlines, legend.
// Everything Leaflet-related lives here, so if the map library fails to load the rest of
// the platform (patients, history, buttons) still works and the map says so.

import { STATE_WORD, dateTime, el, glyph, hazardText, homeState, secondsAgo } from "./common.js";

// Zoomed out enough to see weather coming, not just the streets around each home.
const REGIONAL_ZOOM = 9;
const RADAR_REFRESH_MS = 5 * 60000;
const WARNING_STROKE = "#C9861A";  // one amber for every warning: red on this map only ever means a patient
const SHAPE_ORDER = ["critical", "unreached", "out", "responding", "silent", "on"];  // drawn on top first

export function conditionsText(c) {
  const parts = [];
  if (c.temperature_f !== null) parts.push(`${c.temperature_f}°F`);
  if (c.wind_mph !== null) parts.push(`wind ${c.wind_mph} mph${c.gust_mph ? `, gusts ${c.gust_mph}` : ""}`);
  if (c.thunder_percent) parts.push(`${c.thunder_percent}% chance of thunder`);
  return parts.join(", ");
}

// Jump instead of fly for people who asked the system for less motion; checked per move, as it can change.
const animate = () => !matchMedia("(prefers-reduced-motion: reduce)").matches;

const NO_MAP = { showUnits() {}, showWeather() {}, weatherUnavailable() {}, resize() {}, focus() {}, fitTo() {} };

/** `labelFor(unit)` gives the board's wording for a unit, shared with the list beside the map. */
export function createBoardMap(container, { center, labelFor, onSelect = () => {} }) {
  if (typeof window.L === "undefined") {
    container.replaceChildren(el("p", { class: "map-unavailable" },
      "Map unavailable: the map library didn't load. Everything else on this page still works; reload to try again."));
    return NO_MAP;
  }
  const L = window.L;
  const map = L.map(container, { zoomControl: true }).setView(center, REGIONAL_ZOOM);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { attribution: "© OpenStreetMap contributors", className: "basemap-tiles" }).addTo(map);

  const markers = new Map();
  let fitted = false;
  let lastUnits = [];
  let lastWeather = null;
  let focused = null;

  const radar = L.tileLayer.wms("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows", {
    layers: "conus_bref_qcd", format: "image/png", transparent: true, opacity: 0.6, attribution: "Weather: NOAA NWS", className: "radar-tiles",
  }).addTo(map);
  setInterval(() => radar.setParams({ refreshed: Date.now() }), RADAR_REFRESH_MS);

  const warningLayer = L.geoJSON(null, {
    style: (feature) => {
      const high = feature.properties.level === "high";
      return { color: WARNING_STROKE, weight: 2, dashArray: high ? null : "6 5", fillColor: "#F4B740", fillOpacity: high ? 0.12 : 0.06 };
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

  // A pin is the state's shape on a white disc; power on is the small hollow dot. Elements, not
  // HTML strings: nothing typed by staff ever reaches innerHTML.
  function pinIcon(state) {
    const size = state === "on" ? 24 : 40;  // keep in step with .pin in app.css
    return L.divIcon({ className: "pin-icon", html: el("span", { class: `pin is-${state}` }, glyph(state)), iconSize: [size, size] });
  }

  function placeMarker(unit) {
    const state = homeState(unit);
    let marker = markers.get(unit.device_id);
    if (!marker) {
      marker = L.marker([unit.lat, unit.lon], { keyboard: false }).addTo(map);
      marker.on("click", () => onSelect(unit.device_id));
      markers.set(unit.device_id, marker);
    }
    if (marker.state !== state) {
      marker.setIcon(pinIcon(state));
      marker.setZIndexOffset((SHAPE_ORDER.length - SHAPE_ORDER.indexOf(state)) * 100);
      marker.state = state;
      if (focused === unit.device_id) marker.getElement()?.classList.add("is-focused");
    }
    // A DOM node, not an HTML string: patient names are typed by staff.
    const tip = el("div", { class: "map-tip" }, el("strong", {}, unit.name), el("div", {}, labelFor(unit)),
      unit.conditions ? el("div", {}, conditionsText(unit.conditions)) : null,
      ...unit.hazards.map((h) => el("div", { class: "map-tip-hazard" }, hazardText(h))));
    marker.setLatLng([unit.lat, unit.lon]).bindTooltip(tip, { direction: "right", offset: [14, 0] });
  }

  // The coldest reading right now at a home inside a warning, stated once instead of on every pin.
  function coldest() {
    const readings = lastUnits.filter((u) => u.hazards.length).map((u) => u.conditions?.temperature_f).filter((t) => t !== null && t !== undefined);
    return readings.length ? Math.min(...readings) : null;
  }

  function renderLegend() {
    const weather = lastWeather;
    const node = document.getElementById("map-legend");
    if (!node) return;
    const events = weather ? [...new Map(weather.areas.features.map((f) => [f.properties.event, f.properties.level])).entries()] : [];
    const low = coldest();
    const shapes = ["critical", "out", "unreached", "responding", "silent", "on"];
    node.replaceChildren(...[
      el("strong", {}, weather?.mode === "demo" ? "Weather (demo storm)" : "Weather"),
      el("div", { class: "legend-row legend-extra" }, el("span", { class: "radar-scale", "aria-hidden": "true" }), "Radar: light to heavy"),
      ...(!weather ? [el("div", { class: "muted" }, "Checking the weather")]
        : weather.mode === "off" ? [el("div", { class: "muted" }, "Storm Watch is off")]
        : events.length ? events.map(([event, level]) => el("div", { class: "legend-row" }, el("span", { class: `swatch${level !== "high" ? " is-dashed" : ""}` }), event))
        : [el("div", { class: "muted" }, "No warnings or watches near your homes")]),
      low !== null && events.length ? el("div", { class: "muted legend-extra" }, `Coldest reading now ${low}°F across the warning area`) : null,
      el("strong", { class: "legend-homes-title" }, "Homes"),
      el("ul", { class: "legend-homes" }, shapes.map((state) => el("li", {}, glyph(state), STATE_WORD[state]))),
      weather?.checked_at ? el("div", { class: "muted legend-extra" }, `Checked ${secondsAgo(weather.checked_at)}`) : null,
    ].filter(Boolean));
  }

  return {
    fitTo(units) {
      fitted = true;  // the first-load overview must not undo this
      if (units.length) map.flyToBounds(L.latLngBounds(units.map((u) => [u.lat, u.lon])).pad(0.3), { maxZoom: 12, duration: 0.8, animate: animate() });
    },
    focus(unit) {
      map.flyTo([unit.lat, unit.lon], Math.max(map.getZoom(), 12), { duration: 0.6, animate: animate() });
      if (focused) markers.get(focused)?.getElement()?.classList.remove("is-focused");
      focused = unit.device_id;
      markers.get(unit.device_id)?.getElement()?.classList.add("is-focused");
      markers.get(unit.device_id)?.openTooltip();
    },
    showUnits(units) {
      lastUnits = units;
      units.forEach(placeMarker);
      const live = new Set(units.map((u) => u.device_id));
      for (const [id, marker] of markers) if (!live.has(id)) { marker.remove(); markers.delete(id); }
      renderLegend();
      if (!fitted && units.length) {
        // Once, on first load: every home in view with room around it, then the user is in charge.
        map.fitBounds(L.latLngBounds(units.map((u) => [u.lat, u.lon])).pad(1.5), { maxZoom: REGIONAL_ZOOM });
        fitted = true;
      }
    },
    showWeather(weather) {
      warningLayer.clearLayers();
      warningLayer.addData(weather.areas);
      lastWeather = weather;
      renderLegend();
    },
    weatherUnavailable() {
      document.getElementById("map-legend").replaceChildren(el("strong", {}, "Weather"), el("div", { class: "muted" }, "Weather unavailable, retrying"));
    },
    resize() {
      map.invalidateSize();
    },
  };
}
