// The live board's map: patient homes, radar, warning outlines, temperatures, legend.
// Everything Leaflet-related lives here, so if the map library fails to load the rest of
// the platform (patients, history, buttons) still works and the map says so.

import { dateTime, el, hazardText, secondsAgo, stateOf } from "./common.js";

// Zoomed out enough to see weather coming, not just the streets around each home.
const REGIONAL_ZOOM = 9;
const RADAR_REFRESH_MS = 5 * 60000;
const LABEL_MIN_GAP_PX = 44;

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

export function conditionsText(c) {
  const parts = [];
  if (c.temperature_f !== null) parts.push(`${c.temperature_f}°F`);
  if (c.wind_mph !== null) parts.push(`wind ${c.wind_mph} mph${c.gust_mph ? `, gusts ${c.gust_mph}` : ""}`);
  if (c.thunder_percent) parts.push(`${c.thunder_percent}% chance of thunder`);
  return parts.join(", ");
}

const NO_MAP = { showUnits() {}, showWeather() {}, weatherUnavailable() {}, resize() {}, focus() {}, fitTo() {}, showRanks() {} };

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

  const tokens = getComputedStyle(document.documentElement);
  const markerColor = Object.fromEntries(
    [["out", "--color-out"], ["acked", "--color-acked"], ["on", "--color-on"], ["quiet", "--color-quiet"]]
      .map(([state, token]) => [state, tokens.getPropertyValue(token).trim()]));
  const markers = new Map();
  const tempLabels = new Map();
  const rankPins = new Map();  // device id -> numbered pin, matching the triage list
  let fitted = false;
  let lastUnits = [];

  const radar = L.tileLayer.wms("https://opengeo.ncep.noaa.gov/geoserver/conus/conus_bref_qcd/ows", {
    layers: "conus_bref_qcd", format: "image/png", transparent: true, opacity: 0.6, attribution: "Weather: NOAA NWS",
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

  function placeMarker(unit) {
    const state = stateOf(unit);
    const style = { radius: state === "out" ? 13 : 8, color: markerColor[state], fillColor: markerColor[state], fillOpacity: 0.85, weight: 2 };
    let marker = markers.get(unit.device_id);
    if (!marker) {
      marker = L.circleMarker([unit.lat, unit.lon], style).addTo(map);
      markers.set(unit.device_id, marker);
    }
    // A DOM node, not an HTML string: patient names are typed by staff.
    const tip = el("div", { class: "map-tip" }, el("strong", {}, unit.name), el("div", {}, labelFor(unit)),
      unit.conditions ? el("div", {}, conditionsText(unit.conditions)) : null,
      ...unit.hazards.map((h) => el("div", { class: "map-tip-hazard" }, hazardText(h))));
    marker.setLatLng([unit.lat, unit.lon]).setStyle(style).setRadius(style.radius).bindTooltip(tip);
  }

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
    const swatch = (color, dashed) => el("span", { class: `swatch${dashed ? " is-dashed" : ""}`, style: `--swatch: ${color}` });
    document.getElementById("map-legend").replaceChildren(...[
      el("strong", {}, weather.mode === "demo" ? "Weather (demo storm)" : "Weather"),
      el("div", { class: "legend-row legend-extra" }, el("span", { class: "radar-scale", "aria-hidden": "true" }), "Radar: light to heavy"),
      ...(weather.mode === "off" ? [el("div", { class: "muted" }, "Storm Watch is off")]
        : events.length ? events.map(([event, level]) => el("div", { class: "legend-row" }, swatch(warningColor(event), level !== "high"), event))
        : [el("div", { class: "muted" }, "No warnings or watches near your homes")]),
      weather.checked_at ? el("div", { class: "muted legend-extra" }, `Checked ${secondsAgo(weather.checked_at)}`) : null,
    ].filter(Boolean));
  }

  return {
    /** Numbered pins on the homes that need someone, in triage order; a tap selects that home. */
    showRanks(ranked) {
      const live = new Set(ranked.map((u) => u.device_id));
      for (const [id, pin] of rankPins) if (!live.has(id)) { pin.remove(); rankPins.delete(id); }
      ranked.forEach((unit, index) => {
        const html = `<span class="rank-pin is-${stateOf(unit)}">${index + 1}</span>`;
        let pin = rankPins.get(unit.device_id);
        if (!pin) {
          pin = L.marker([unit.lat, unit.lon], { keyboard: false, zIndexOffset: 1000 }).addTo(map);
          pin.on("click", () => onSelect(unit.device_id));
          rankPins.set(unit.device_id, pin);
        }
        pin.setLatLng([unit.lat, unit.lon]).setIcon(L.divIcon({ className: "rank-pin-icon", html, iconSize: [26, 26] }));
      });
    },
    fitTo(units) {
      fitted = true;  // the first-load overview must not undo this
      if (units.length) map.flyToBounds(L.latLngBounds(units.map((u) => [u.lat, u.lon])).pad(0.3), { maxZoom: 12, duration: 0.8 });
    },
    focus(unit) {
      map.flyTo([unit.lat, unit.lon], Math.max(map.getZoom(), 12), { duration: 0.6 });
      markers.get(unit.device_id)?.openTooltip();
    },
    showUnits(units) {
      lastUnits = units;
      units.forEach(placeMarker);
      const live = new Set(units.map((u) => u.device_id));
      for (const [id, marker] of markers) if (!live.has(id)) { marker.remove(); markers.delete(id); }
      placeTemperatures(units);
      if (!fitted && units.length) {
        // Once, on first load: every home in view with room around it, then the user is in charge.
        map.fitBounds(L.latLngBounds(units.map((u) => [u.lat, u.lon])).pad(1.5), { maxZoom: REGIONAL_ZOOM });
        fitted = true;
      }
    },
    showWeather(weather) {
      warningLayer.clearLayers();
      warningLayer.addData(weather.areas);
      renderLegend(weather);
    },
    weatherUnavailable() {
      document.getElementById("map-legend").replaceChildren(el("strong", {}, "Weather"), el("div", { class: "muted" }, "Weather unavailable, retrying"));
    },
    resize() {
      map.invalidateSize();
    },
  };
}
