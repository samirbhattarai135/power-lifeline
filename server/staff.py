"""Staff sign-in. A StaffAuthenticator checks an email + password against
whatever identity provider is configured; Sessions then hands out our own
opaque, expiring session ids so no provider token ever reaches the browser."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

SESSION_LIFETIME = timedelta(hours=12)


@dataclass(frozen=True)
class StaffMember:
    email: str


class StaffAuthenticator(Protocol):
    def sign_in(self, email: str, password: str) -> StaffMember | None:
        """The staff member if the credentials are valid, otherwise None."""
        ...


class Sessions:
    # ponytail: in-memory; staff sign in again after a server restart. Persist if that annoys anyone.
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[StaffMember, datetime]] = {}

    def start(self, member: StaffMember) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = (member, datetime.now(timezone.utc) + SESSION_LIFETIME)
        return session_id

    def member(self, session_id: str | None) -> StaffMember | None:
        if not session_id:
            return None
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            member, expires_at = entry
            if datetime.now(timezone.utc) >= expires_at:
                del self._sessions[session_id]
                return None
            return member

    def end(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)


class LoginThrottle:
    """Slows password guessing: after MAX_FAILURES failed sign-ins from one address
    within WINDOW, further attempts from it are refused until the window passes."""

    MAX_FAILURES = 10
    WINDOW = timedelta(minutes=15)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[datetime]] = {}

    def is_blocked(self, address: str) -> bool:
        with self._lock:
            return len(self._recent(address)) >= self.MAX_FAILURES

    def record_failure(self, address: str) -> None:
        with self._lock:
            self._failures[address] = self._recent(address) + [datetime.now(timezone.utc)]

    def record_success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)

    def _recent(self, address: str) -> list[datetime]:
        cutoff = datetime.now(timezone.utc) - self.WINDOW
        return [t for t in self._failures.get(address, []) if t > cutoff]
