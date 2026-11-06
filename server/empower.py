"""HHS emPOWER adapter: how many Medicare beneficiaries in a county rely on electricity-dependent
equipment, from HHS's public, de-identified, monthly REST service (no key needed).
https://empowerprogram.hhs.gov/empower-rest.html"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from service import CountsUnavailable, CountyFigures

COUNTY_LAYER = ("https://services2.arcgis.com/ZQ4jTQn6k7VPXEwO/arcgis/rest/services/"
                "HHS_emPOWER_REST_Service_Public/FeatureServer/2/query")
FIELDS = "NAMELSAD,State,FIPS_Code,Power_Dependent_Devices_DME,Home_Health_Services_Any_DME,O2_Services_Any_DME"
TIMEOUT_SECONDS = 15


class EmpowerCounts:
    def county_at(self, lat: float, lon: float) -> CountyFigures:
        # Rounded to about a kilometre: enough to find the county, without sending a home's exact spot.
        query = urllib.parse.urlencode({
            "geometry": f"{round(lon, 2)},{round(lat, 2)}", "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": FIELDS, "returnGeometry": "false", "f": "json",
        })
        try:
            with urllib.request.urlopen(f"{COUNTY_LAYER}?{query}", timeout=TIMEOUT_SECONDS) as response:
                return county_from_response(json.load(response))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise CountsUnavailable(f"emPOWER unreachable: {getattr(error, 'reason', error)}") from error


def county_from_response(data: dict) -> CountyFigures:
    features = data.get("features") or []
    if not features:
        raise CountsUnavailable(f"emPOWER has no county here: {str(data.get('error', 'no match'))[:200]}")
    a = features[0]["attributes"]
    try:
        return CountyFigures(fips=a["FIPS_Code"], name=f"{a['NAMELSAD']}, {a['State']}",
                             power_dependent=int(a["Power_Dependent_Devices_DME"]),
                             home_health=int(a["Home_Health_Services_Any_DME"]), oxygen=int(a["O2_Services_Any_DME"]))
    except (KeyError, TypeError, ValueError) as error:
        raise CountsUnavailable(f"emPOWER answer changed shape: {error!r}") from None
