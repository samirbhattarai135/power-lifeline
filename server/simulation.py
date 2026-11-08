"""A storm you can start from the board, for the demo board of synthetic homes only
(LIFELINE_STORM_SIM=1). Homes lose power one after another through the same report use case
real units go through, so escalation, Storm Watch and the county view behave exactly as live.
While it runs, every home keeps reporting its simulated state, so none of them goes silent."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from domain import Patient, UnknownDevice
from service import Lifeline

log = logging.getLogger("lifeline.simulation")

HEARTBEAT_SECONDS = 20.0
MAX_EVERY_SECONDS = 60.0
POWERED_BATTERY, OUT_BATTERY = 95, 88


class SimulationRefused(ValueError):
    """The simulation won't run; the message is shown to staff."""


# Waits up to `seconds`; True means the run was cancelled. Tests pass one that doesn't sleep.
Pause = Callable[[threading.Event, float], bool]


def _wait(cancel: threading.Event, seconds: float) -> bool:
    return cancel.wait(seconds)


class StormSimulation:
    def __init__(self, lifeline: Lifeline, heartbeat_seconds: float = HEARTBEAT_SECONDS, pause: Pause = _wait):
        self._lifeline = lifeline
        self._heartbeat_seconds = heartbeat_seconds
        self._pause = pause
        self._restarting = threading.Lock()  # one run at a time; never held by the run itself
        self._lock = threading.Lock()        # the progress below
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._dark: set[str] = set()
        self._planned = 0

    def status(self) -> dict:
        homes = len(self._lifeline.roster())
        with self._lock:
            return {"homes": homes, "out": len(self._dark), "planned": self._planned,
                    "running": self._thread is not None and self._thread.is_alive()}

    def start_storm(self, homes: int, every_seconds: float) -> dict:
        """`homes` homes lose power, `every_seconds` apart, after every home first reports power on,
        so starting again restarts the storm from now instead of continuing an old one."""
        roster = self._synthetic_roster()
        if isinstance(homes, bool) or not isinstance(homes, int) or not 1 <= homes <= len(roster):
            raise SimulationRefused(f"Choose between 1 and {len(roster)} homes.")
        if isinstance(every_seconds, bool) or not isinstance(every_seconds, (int, float)) or not 0 <= every_seconds <= MAX_EVERY_SECONDS:
            raise SimulationRefused(f"Choose 0 to {MAX_EVERY_SECONDS:g} seconds between homes.")
        self._restart(roster, roster[:homes], float(every_seconds))
        return self.status()

    def calm(self) -> dict:
        """Every home has power again; heartbeats carry on."""
        self._restart(self._synthetic_roster(), [], 0.0)
        return self.status()

    def wait_until_stopped(self, timeout: float = 5.0) -> None:
        """For tests and shutdown."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _synthetic_roster(self) -> list[Patient]:
        roster = sorted(self._lifeline.roster(), key=lambda p: p.device_id)
        real = [p for p in roster if not p.is_synthetic()]
        if real:
            raise SimulationRefused(f"Refused: {len(real)} home(s) here are not synthetic. "
                                    "The simulation only runs on a board of synthetic homes.")
        if not roster:
            raise SimulationRefused("There are no homes to simulate yet.")
        return roster

    def _restart(self, roster: list[Patient], out: list[Patient], every_seconds: float) -> None:
        with self._restarting:
            self._cancel.set()
            if self._thread is not None:
                self._thread.join(timeout=5)
            with self._lock:
                self._cancel = threading.Event()
                self._dark = set()
                self._planned = len(out)
            for patient in roster:  # before returning, so the next board refresh already shows the reset
                self._send(patient, powered=True)
            self._thread = threading.Thread(target=self._run, args=(roster, out, every_seconds, self._cancel),
                                            daemon=True, name="storm-simulation")
            self._thread.start()

    def _run(self, roster: list[Patient], out: list[Patient], every_seconds: float, cancel: threading.Event) -> None:
        for index, patient in enumerate(out):
            if index and self._pause(cancel, every_seconds):
                return
            self._send(patient, powered=False)
            with self._lock:
                self._dark.add(patient.device_id)
        while not self._pause(cancel, self._heartbeat_seconds):
            with self._lock:
                dark = set(self._dark)
            for patient in roster:
                self._send(patient, powered=patient.device_id not in dark)

    def _send(self, patient: Patient, powered: bool) -> None:
        try:
            self._lifeline.report(patient.device_id, powered, POWERED_BATTERY if powered else OUT_BATTERY)
        except UnknownDevice:
            pass  # removed from the roster mid-run
