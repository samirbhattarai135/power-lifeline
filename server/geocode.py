"""Address to map location, using the US Census Bureau geocoder (free, no key).

Saves staff from looking up each patient's latitude and longitude by hand.
The address does leave our server, to a US government service with no account."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
TIMEOUT_SECONDS = 10
MAX_ADDRESS_LENGTH = 200


@dataclass(frozen=True)
class Match:
    address: str  # as the Census Bureau spells it, so staff can confirm it's the right place
    lat: float
    lon: float


class GeocoderUnavailable(RuntimeError):
    pass


class CensusGeocoder:
    def locate(self, address: str) -> list[Match]:
        query = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
        try:
            with urllib.request.urlopen(f"{API}?{query}", timeout=TIMEOUT_SECONDS) as response:
                return matches_from_response(json.load(response))
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as error:
            raise GeocoderUnavailable(f"address lookup failed: {error}") from error


def matches_from_response(body: dict) -> list[Match]:
    return [Match(m["matchedAddress"], float(m["coordinates"]["y"]), float(m["coordinates"]["x"]))
            for m in body["result"]["addressMatches"]]
