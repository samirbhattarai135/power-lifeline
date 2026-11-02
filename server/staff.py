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
    """Slows password guessing on two keys at once:
    - one account from one address: MAX_PER_ACCOUNT failures per WINDOW
    - one address across all accounts: MAX_PER_ADDRESS failures per WINDOW,
      so rotating email addresses (or, in shared-password mode, where the email
      is not part of the secret) does not reset the count."""

    MAX_PER_ACCOUNT = 10
    MAX_PER_ADDRESS = 30
    WINDOW = timedelta(minutes=15)
    MAX_TRACKED_KEYS = 10_000  # bounds memory under a flood of distinct keys

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[datetime]] = {}

    def is_blocked(self, address: str, email: str) -> bool:
        with self._lock:
            return (len(self._recent(self._account_key(address, email))) >= self.MAX_PER_ACCOUNT
                    or len(self._recent(address)) >= self.MAX_PER_ADDRESS)

    def record_failure(self, address: str, email: str) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            if len(self._failures) >= self.MAX_TRACKED_KEYS:
                self._forget_expired()
            for key in (address, self._account_key(address, email)):
                self._failures[key] = self._recent(key) + [now]

    def record_success(self, address: str, email: str) -> None:
        # Only the account key resets; a success must not wipe an address's guessing history.
        with self._lock:
            self._failures.pop(self._account_key(address, email), None)

    @staticmethod
    def _account_key(address: str, email: str) -> str:
        return f"{address}|{email}"

    def _recent(self, key: str) -> list[datetime]:
        cutoff = datetime.now(timezone.utc) - self.WINDOW
        return [t for t in self._failures.get(key, []) if t > cutoff]

    def _forget_expired(self) -> None:
        for key in [k for k in self._failures if not self._recent(k)]:
            del self._failures[key]
        if len(self._failures) >= self.MAX_TRACKED_KEYS:  # still full: drop the stalest half
            stalest = sorted(self._failures, key=lambda k: self._failures[k][-1])
            for key in stalest[: len(stalest) // 2]:
                del self._failures[key]
