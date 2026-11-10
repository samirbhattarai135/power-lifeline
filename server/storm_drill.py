"""Drives the separate storm-demo board (a Lifeline in local-file mode with synthetic homes only):
creates the homes once, then knocks 16 of them out or brings them all back. It talks to the app's
public API like real units and staff would, so it needs no server changes.

  read -rs STORM_ADMIN_PASSWORD && export STORM_ADMIN_PASSWORD
  python3 server/storm_drill.py https://<storm host> setup --admin you@example.org
  python3 server/storm_drill.py https://<storm host> storm --admin you@example.org [--spread 2]
  python3 server/storm_drill.py https://<storm host> calm  --admin you@example.org

Refuses to touch a server whose roster has any home not created by this script, so it can never
put fake outages on the real board.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie

PREFIX = "storm-"
TIMEOUT_SECONDS = 15
NURSE = {"role": "agency", "name": "Home health on-call nurse", "phone": "+16015559190", "channel": "call"}
# (first name, town, lat, lon, equipment, backup minutes, acuity). Short backups make the "under
# 15 minutes left" tier appear about 10 minutes after the storm starts.
HOMES = [
    ("Ruth", "Hattiesburg", 31.327, -89.290, "Oxygen concentrator", 240, 1),
    ("James", "Petal", 31.347, -89.260, "Home ventilator", 25, 1),
    ("Linda", "Purvis", 31.143, -89.410, "BiPAP machine", 60, 2),
    ("Dorothy", "Sumrall", 31.417, -89.542, "Suction machine", 90, 1),
    ("Earl", "Lumberton", 31.001, -89.452, "Powered air mattress", 480, 3),
    ("Mae", "Columbia", 31.252, -89.837, "Enteral feeding pump", 720, 2),
    ("Walter", "Laurel", 31.694, -89.131, "Home hemodialysis machine", 30, 1),
    ("Gloria", "Ellisville", 31.604, -89.196, "Infusion pump", 480, 2),
    ("Henry", "Wiggins", 30.858, -89.135, "Oxygen concentrator", 20, 1),
    ("Bessie", "Seminary", 31.556, -89.495, "Home ventilator", 120, 1),
    ("Tom", "Richton", 31.349, -88.940, "BiPAP machine", 25, 2),
    ("Alma", "Oak Grove", 31.313, -89.400, "Oxygen concentrator", 180, 1),
    ("Otis", "Hattiesburg", 31.300, -89.330, "Suction machine", 45, 1),
    ("Hazel", "Petal", 31.360, -89.230, "Oxygen concentrator", 300, 2),
    ("Leroy", "Purvis", 31.160, -89.380, "Home ventilator", 60, 1),
    ("Mildred", "Laurel", 31.670, -89.160, "Powered air mattress", 600, 3),
    ("Clyde", "Columbia", 31.230, -89.810, "Oxygen concentrator", 240, 2),
    ("Ida", "Sumrall", 31.430, -89.520, "Infusion pump", 360, 2),
]
OUT_COUNT = 16


def home_form(index: int) -> dict:
    name, town, lat, lon, equipment, backup, acuity = HOMES[index]
    n = index + 1
    return {
        "device_id": f"{PREFIX}{n:02d}", "name": f"{name} {chr(65 + index)}. (synthetic)",
        "address": f"{100 + n * 7} Main St, {town}, MS", "lat": lat, "lon": lon, "equipment": equipment,
        "backup_minutes": backup, "acuity": acuity,
        "contacts": [{"role": "caregiver", "name": "Daughter", "phone": f"+1601555{1000 + n * 10:04d}", "channel": "sms"}, NURSE],
        "plan": {"supplies": "Spare battery charged; flashlight by the bed", "place_name": "Forrest General Hospital",
                 "place_address": "6051 US Highway 49, Hattiesburg, MS", "steps": "Switch to backup\nCall the nurse"},
    }


class Board:
    def __init__(self, base: str, admin: str, password: str):
        self.base = base.rstrip("/")
        status, headers, body = self.call("POST", "/api/login", {"email": admin, "password": password})
        if status != 200:
            raise SystemExit(f"sign-in refused (HTTP {status}): {body[:200]!r}")
        self.cookie = SimpleCookie(headers.get("Set-Cookie", "")).output(attrs=[], header="").strip()

    def call(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
        data = json.dumps(payload).encode() if payload is not None else None
        all_headers = {"Content-Type": "application/json", **({"Cookie": self.cookie} if getattr(self, "cookie", None) else {}),
                       **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, headers=all_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read()

    def roster(self) -> list[dict]:
        status, _, body = self.call("GET", "/api/patients")
        if status != 200:
            raise SystemExit(f"can't read the roster (HTTP {status}); is this account an admin?")
        patients = json.loads(body)["patients"]
        strangers = [p["device_id"] for p in patients if not p["device_id"].startswith(PREFIX)]
        if strangers:
            raise SystemExit(f"refusing: this server has homes this script didn't create ({len(strangers)}). "
                             "Is this the real board?")
        return patients

    def report(self, patient: dict, wall_power: bool) -> None:
        body = {"device_id": patient["device_id"], "wall_power": wall_power, "battery_percent": 95 if wall_power else 88}
        status, _, _ = self.call("POST", "/api/report", body, {"X-Device-Token": patient["device_token"]})
        if status != 200:
            print(f"  {patient['device_id']}: HTTP {status}", file=sys.stderr)


def setup(board: Board) -> None:
    existing = {p["device_id"] for p in board.roster()}
    for index in range(len(HOMES)):
        form = home_form(index)
        if form["device_id"] in existing:
            continue
        status, _, body = board.call("POST", "/api/patients", form)
        if status != 201:
            raise SystemExit(f"adding {form['device_id']} failed (HTTP {status}): {body[:200]!r}")
    print(f"{len(board.roster())} synthetic homes on the storm board")


def storm(board: Board, spread_seconds: float) -> None:
    patients = sorted(board.roster(), key=lambda p: p["device_id"])
    for patient in patients:  # every home reports power on first, so a rerun restarts the outages from now
        board.report(patient, True)
    for patient in patients[:OUT_COUNT]:
        board.report(patient, False)
        time.sleep(spread_seconds)
    print(f"storm: {min(OUT_COUNT, len(patients))} of {len(patients)} homes without power")


def calm(board: Board) -> None:
    patients = board.roster()
    for patient in patients:
        board.report(patient, True)
    print(f"calm: all {len(patients)} homes have power")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base_url")
    parser.add_argument("command", choices=("setup", "storm", "calm"))
    parser.add_argument("--admin", required=True, help="the storm app's LIFELINE_BOOTSTRAP_ADMIN email")
    parser.add_argument("--spread", type=float, default=0.0, help="seconds between homes going dark (a live wave)")
    args = parser.parse_args()
    password = os.environ.get("STORM_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("set STORM_ADMIN_PASSWORD (read -rs STORM_ADMIN_PASSWORD && export STORM_ADMIN_PASSWORD)")
    board = Board(args.base_url, args.admin, password)
    if args.command == "setup":
        setup(board)
    elif args.command == "storm":
        storm(board, args.spread)
    else:
        calm(board)


if __name__ == "__main__":
    main()
