"""Staff sign-in. A StaffAuthenticator checks an email + password against
whatever identity provider is configured; Sessions then hands out our own
signed, expiring session cookies so no provider token ever reaches the browser."""

from __future__ import annotations

import base64
import hashlib
import hmac
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


MIN_PASSWORD_LENGTH = 10


class PasswordChangeRefused(ValueError):
    """The new password was not accepted; the message tells the person why, in plain words."""


class SignInAccounts(Protocol):
    """Creating and changing the sign-in accounts themselves (roles live in access.py)."""

    def create(self, email: str, temporary_password: str) -> bool:
        """Make the sign-in account, or take over an existing one, so that `temporary_password` is now
        the only password that works for it. False when accounts aren't individual (shared password)."""
        ...

    def change_password(self, email: str, current_password: str, new_password: str) -> bool:
        """False if the current password is wrong; raises PasswordChangeRefused if the new one is refused."""
        ...


class IdentityProvider(StaffAuthenticator, SignInAccounts, Protocol):
    """Signs people in and manages their accounts: Supabase Auth, or the local shared password."""


def temporary_password() -> str:
    """Shown once to the admin, who passes it on; the person replaces it after signing in."""
    return secrets.token_urlsafe(12)


def check_new_password(current: str, new: str) -> None:
    if len(new) < MIN_PASSWORD_LENGTH:
        raise PasswordChangeRefused(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if new == current:
        raise PasswordChangeRefused("Choose a password different from the current one.")


class Sessions:
    """Signed, expiring session cookies: "email|expiry|session id" plus an HMAC under a server secret.
    Nothing is stored server-side, so staff stay signed in across restarts; the secret
    (LIFELINE_SESSION_SECRET) is what makes a cookie impossible to forge or alter.
    Signing out clears the cookie and remembers it as revoked until it would have expired.
    ponytail: that revoked list is in memory, so a signed-out cookie copied before sign-out
    works again after a restart, until its 12 hours run out. Store revocations if that matters."""

    MIN_SECRET_LENGTH = 32

    def __init__(self, secret: str):
        if len(secret) < self.MIN_SECRET_LENGTH:
            raise ValueError(f"LIFELINE_SESSION_SECRET must be at least {self.MIN_SECRET_LENGTH} characters")
        self._key = secret.encode()
        self._lock = threading.Lock()
        self._revoked: dict[str, datetime] = {}  # session id -> when it would have expired anyway

    def start(self, member: StaffMember, now: datetime | None = None) -> str:
        expires_at = (now or datetime.now(timezone.utc)) + SESSION_LIFETIME
        payload = f"{member.email}|{int(expires_at.timestamp())}|{secrets.token_urlsafe(16)}".encode()
        return f"{_b64(payload)}.{_b64(self._sign(payload))}"

    def member(self, token: str | None, now: datetime | None = None) -> StaffMember | None:
        now = now or datetime.now(timezone.utc)
        parsed = self._parse(token)
        if parsed is None:
            return None
        email, expires_at, session_id = parsed
        with self._lock:
            if session_id in self._revoked:
                return None
        return StaffMember(email) if now < expires_at else None

    def end(self, token: str | None) -> None:
        parsed = self._parse(token)
        if parsed is None:
            return
        now = datetime.now(timezone.utc)
        with self._lock:
            self._revoked = {i: e for i, e in self._revoked.items() if e > now}  # forget long-dead sessions
            # By id, not by cookie text: a re-encoded copy of the same cookie stays signed out too.
            self._revoked[parsed[2]] = parsed[1]

    def _parse(self, token: str | None) -> tuple[str, datetime, str] | None:
        if not token:
            return None
        try:
            payload_part, signature_part = token.split(".")
            payload = _unb64(payload_part)
            if not hmac.compare_digest(_unb64(signature_part), self._sign(payload)):
                return None
            email, expires_text, session_id = payload.decode().rsplit("|", 2)
            return email, datetime.fromtimestamp(int(expires_text), timezone.utc), session_id
        except (ValueError, UnicodeDecodeError):
            return None

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    """Strict: only the one canonical spelling of each value decodes, so a token can't be
    re-encoded into a different string that still verifies."""
    raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    if _b64(raw) != text:
        raise ValueError("non-canonical encoding")
    return raw


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
