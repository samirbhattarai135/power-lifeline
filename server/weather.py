"""National Weather Service adapter for Storm Watch (api.weather.gov: free, no key).

NWS asks every client to identify itself with a User-Agent that includes contact
details, so set NWS_USER_AGENT to something like "Lifeline (you@example.org)".
Parsing is kept separate from fetching so it can be tested without the network."""

from __future__ import annotations

import json
import math
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

from storm import (ALERT_LEVELS, KMH_PER_MPH, Conditions, ForecastPeriod, Hazard, RiskLevel, WarningArea,
                   WeatherReport, WeatherUnavailable, alert_hazard, assess_forecast)

API = "https://api.weather.gov"
TIMEOUT_SECONDS = 10
DURATION_PATTERN = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


class NwsWeather:
    def __init__(self, user_agent: str):
        self._headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
        # Neither a home's forecast grid nor a zone's outline moves, so both are fetched once.
        self._grid_urls: dict[tuple[float, float], str] = {}
        self._zone_shapes: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def report_at(self, lat: float, lon: float, now: datetime) -> WeatherReport:
        point = (round(lat, 4), round(lon, 4))  # NWS rejects more than 4 decimal places
        alerts = self._get(f"{API}/alerts/active?point={point[0]},{point[1]}")
        grid = self._get(self._grid_url(point))
        try:
            return WeatherReport(
                hazards=hazards_from_alerts(alerts) + hazards_from_grid(grid, now),
                conditions=conditions_from_grid(grid, now),
                areas=areas_from_alerts(alerts, self._zone_shape),
            )
        except (KeyError, ValueError, TypeError) as error:
            raise WeatherUnavailable(f"unexpected weather.gov data: {error}") from error

    def _grid_url(self, point: tuple[float, float]) -> str:
        with self._lock:
            cached = self._grid_urls.get(point)
        if cached:
            return cached
        url = self._get(f"{API}/points/{point[0]},{point[1]}")["properties"]["forecastGridData"]
        with self._lock:
            self._grid_urls[point] = url
        return url

    def _zone_shape(self, zone_url: str) -> dict | None:
        """Outline of a forecast zone or county. Many warnings (ice, wind, heat) are issued
        for whole zones and carry no shape of their own."""
        with self._lock:
            if zone_url in self._zone_shapes:
                return self._zone_shapes[zone_url]
        try:
            shape = self._get(zone_url).get("geometry")
        except WeatherUnavailable:
            return None  # draw what we can; try again next round
        with self._lock:
            self._zone_shapes[zone_url] = shape
        return shape

    def _get(self, url: str) -> dict:
        # Zone and grid URLs come from NWS responses; never follow one off weather.gov.
        if not url.startswith(API + "/"):
            raise WeatherUnavailable(f"refusing to fetch outside weather.gov: {url}")
        request = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
            raise WeatherUnavailable(f"weather.gov request failed: {error}") from error


def hazards_from_alerts(collection: dict) -> list[Hazard]:
    return [alert_hazard(p["id"], p["event"], starts, ends) for p, starts, ends in _relevant_alerts(collection)]


def areas_from_alerts(collection: dict, zone_shape: Callable[[str], dict | None]) -> list[WarningArea]:
    areas = []
    for feature in collection.get("features", []):
        p = feature.get("properties", {})
        times = _alert_times(p)
        if times is None or p["event"] not in ALERT_LEVELS:
            continue
        geometry = feature.get("geometry") or _merged_zones(p.get("affectedZones", []), zone_shape)
        if geometry is not None:
            areas.append(WarningArea(id=p["id"], event=p["event"], level=ALERT_LEVELS[p["event"]],
                                     ends=times[1], geometry=geometry))
    return areas


def conditions_from_grid(grid: dict, now: datetime) -> Conditions:
    layers = grid.get("properties", {})
    temperature_c = _value_now(layers.get("temperature"), "wmoUnit:degC", now)
    wind_kmh = _value_now(layers.get("windSpeed"), "wmoUnit:km_h-1", now)
    gust_kmh = _value_now(layers.get("windGust"), "wmoUnit:km_h-1", now)
    thunder = _value_now(layers.get("probabilityOfThunder"), None, now)
    return Conditions(
        temperature_f=None if temperature_c is None else round(temperature_c * 9 / 5 + 32),
        wind_mph=None if wind_kmh is None else round(wind_kmh / KMH_PER_MPH),
        gust_mph=None if gust_kmh is None else round(gust_kmh / KMH_PER_MPH),
        thunder_percent=None if thunder is None else round(thunder),
    )


def hazards_from_grid(grid: dict, now: datetime) -> list[Hazard]:
    layers = grid.get("properties", {})
    gusts = _periods(layers.get("windGust"), expected_unit="wmoUnit:km_h-1")
    ice = _periods(layers.get("iceAccumulation"), expected_unit="wmoUnit:mm")
    return assess_forecast(gusts, ice, now)


def parse_duration(text: str) -> timedelta:
    """The ISO 8601 durations NWS uses: PT1H, PT6H, P1D, P1DT12H."""
    match = DURATION_PATTERN.match(text)
    if not match:
        raise ValueError(f"unsupported duration {text!r}")
    days, hours, minutes = (int(group or 0) for group in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes)


# ---- helpers ------------------------------------------------------------------------

def _alert_times(p: dict) -> tuple[datetime, datetime] | None:
    starts = p.get("onset") or p.get("effective")
    ends = p.get("ends") or p.get("expires")
    if not (p.get("id") and p.get("event") and starts and ends):
        return None
    return datetime.fromisoformat(starts), datetime.fromisoformat(ends)


def _relevant_alerts(collection: dict):
    for feature in collection.get("features", []):
        p = feature.get("properties", {})
        times = _alert_times(p)
        if times is not None and p["event"] in ALERT_LEVELS:
            yield p, times[0], times[1]


def _merged_zones(zone_urls: list[str], zone_shape: Callable[[str], dict | None]) -> dict | None:
    polygons = []
    for url in zone_urls:
        shape = zone_shape(url)
        if shape is None:
            continue
        if shape.get("type") == "Polygon":
            polygons.append(shape["coordinates"])
        elif shape.get("type") == "MultiPolygon":
            polygons.extend(shape["coordinates"])
    return {"type": "MultiPolygon", "coordinates": polygons} if polygons else None


def _periods(layer: dict | None, expected_unit: str | None) -> list[ForecastPeriod]:
    # A unit we don't expect means misreading the numbers; better no signal than a wrong one.
    if not layer or layer.get("uom") != expected_unit:
        return []
    periods = []
    for entry in layer.get("values", []):
        if entry.get("value") is None:
            continue
        start_text, _, duration_text = entry["validTime"].partition("/")
        periods.append(ForecastPeriod(datetime.fromisoformat(start_text), parse_duration(duration_text), entry["value"]))
    return periods


def _value_now(layer: dict | None, expected_unit: str | None, now: datetime) -> float | None:
    for period in _periods(layer, expected_unit):
        if period.starts <= now < period.ends:
            return period.value
    return None


# ---- demo ------------------------------------------------------------------------------

DEMO_RADIUS_KM = 40


class DemoWeather:
    """Pretend ice storm over the homes, for showing Storm Watch on a clear day
    (LIFELINE_STORM_WATCH=demo). Everything it says is labelled "(demo)"."""

    def __init__(self, started: datetime):
        self._started = started
        self._center: tuple[float, float] | None = None  # drawn around the first home asked about

    def report_at(self, lat: float, lon: float, now: datetime) -> WeatherReport:
        self._center = self._center or (lat, lon)
        # One id per day, so restarting the server mid-demo doesn't announce the storm again.
        hazard_id = f"demo:{self._started.date().isoformat()}"
        ends = self._started + timedelta(hours=8)
        return WeatherReport(
            hazards=[Hazard(id=hazard_id, level=RiskLevel.HIGH, headline="Ice Storm Warning (demo)",
                            starts=self._started, ends=ends)],
            conditions=Conditions(temperature_f=29, wind_mph=22, gust_mph=41, thunder_percent=0),
            areas=[WarningArea(id=hazard_id, event="Ice Storm Warning (demo)", level=RiskLevel.HIGH, ends=ends,
                               geometry=_circle(*self._center, DEMO_RADIUS_KM))],
        )


def _circle(lat: float, lon: float, radius_km: float, points: int = 48) -> dict:
    km_per_degree = 111.32
    ring = []
    for n in range(points + 1):
        angle = 2 * math.pi * n / points
        ring.append([round(lon + radius_km * math.cos(angle) / (km_per_degree * math.cos(math.radians(lat))), 5),
                     round(lat + radius_km * math.sin(angle) / km_per_degree, 5)])
    return {"type": "Polygon", "coordinates": [ring]}
