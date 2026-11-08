"""Post-deploy smoke test: is the server up, serving pages, accepting a unit's report,
and showing the family page? Stdlib only; safe to run against the live server.

  python3 smoke_test.py https://<app>.fly.dev
  SMOKE_DEVICE_TOKEN=... python3 smoke_test.py https://<app>.fly.dev --device-id smoke-01 --family-token <link token>

The synthetic report always says wall power is ON, so it never opens a power outage. But a unit
that reported once and then goes quiet raises a "unit is silent" alert to its call list after
SILENT_ALERT_AFTER_SECONDS (5 min): use a dedicated test unit and remove it (or run the real unit)
within 5 minutes of the test. With ntfy, every alert lands on the one demo-phone topic.
The device token comes from SMOKE_DEVICE_TOKEN so it stays out of shell history.
Exit status is the number of failed checks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 15  # a cold TLS handshake plus a Supabase round trip can take a few seconds


def request(base: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    all_headers = {"Content-Type": "application/json", **(headers or {})} if data else dict(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=all_headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, dict(error.headers), error.read()


def post_form(base: str, path: str, fields: dict) -> int:
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as error:
        with error:
            return error.code


def is_loopback(base: str) -> bool:
    return urllib.parse.urlsplit(base).hostname in ("localhost", "127.0.0.1", "::1")


class Checks:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.failures += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}{'' if passed else f'  ({detail})'}")


def is_html(status: int, headers: dict) -> bool:
    return status == 200 and headers.get("Content-Type", "").startswith("text/html")


def run(base: str, device_id: str | None, device_token: str | None, family_token: str | None) -> int:
    checks = Checks()
    try:
        status, _, body = request(base, "/healthz")
    except OSError as error:
        checks.check("server reachable", False, str(error))
        return checks.failures
    checks.check("/healthz says ok", status == 200 and json.loads(body or b"{}").get("ok") is True, f"HTTP {status}")

    status, headers, _ = request(base, "/")
    checks.check("landing page (signed out)", is_html(status, headers), f"HTTP {status}")
    checks.check("strict CSP header", "script-src 'self'" in headers.get("Content-Security-Policy", ""), "missing or changed")
    status, headers, _ = request(base, "/login")
    checks.check("sign-in page", is_html(status, headers), f"HTTP {status}")
    status, _, _ = request(base, "/api/status")
    checks.check("board API refuses signed-out callers", status == 401, f"HTTP {status}")
    if not is_loopback(base):
        # Unsigned "OK" replies are trusted only from the server machine. If a proxy on the host made every
        # caller look local, anyone could silence an alert. The number is on no call list, so a 200 changes nothing.
        status = post_form(base, "/api/sms-inbound", {"From": "+10000000000", "Body": "OK"})
        checks.check("forged SMS reply from the internet is refused", status == 403, f"HTTP {status}: unsigned replies are trusted")

    if device_id and device_token:
        report = {"device_id": device_id, "wall_power": True, "battery_percent": 100, "fw": "smoke-test"}
        status, _, _ = request(base, "/api/report", report, {"X-Device-Token": "wrong-" + device_token})
        checks.check("unit report with a wrong token is refused", status == 401, f"HTTP {status}")
        status, _, body = request(base, "/api/report", report, {"X-Device-Token": device_token})
        checks.check("unit report (wall power on) accepted", status == 200, f"HTTP {status} {body[:120]!r}")
    else:
        print("SKIP  unit report (pass --device-id and set SMOKE_DEVICE_TOKEN)")

    if family_token:
        status, headers, _ = request(base, f"/s/{family_token}")
        checks.check("family page", is_html(status, headers), f"HTTP {status}")
        status, _, body = request(base, f"/api/family/{family_token}")
        family = json.loads(body or b"{}") if status == 200 else {}
        checks.check("family status API", status == 200 and "status" in family, f"HTTP {status}")
        if device_id and device_token:
            checks.check("family page sees the unit's report", family.get("last_seen") is not None,
                         "last_seen empty: is the family link for the same test unit?")
    else:
        print("SKIP  family page (pass --family-token)")
    return checks.failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url", nargs="?", default="http://localhost:8080")
    parser.add_argument("--device-id", help="a dedicated test unit, e.g. smoke-01")
    parser.add_argument("--family-token", help="the part after /s/ in that unit's family link")
    args = parser.parse_args()
    failures = run(args.base_url.rstrip("/"), args.device_id, os.environ.get("SMOKE_DEVICE_TOKEN"), args.family_token)
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    sys.exit(min(failures, 100))


if __name__ == "__main__":
    main()
