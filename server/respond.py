"""Signed one-tap "I'm responding" links for push alerts.

A token names one incident and one entry in its alerted list, signed with a
server secret. It carries no name, phone number or address, so it is safe in a
URL, and it can't be forged or pointed at another person's alert. The incident
being closed or already acknowledged is what makes an old token stop working."""

from __future__ import annotations

import base64
import hashlib
import hmac

SIGNATURE_BYTES = 16
MIN_SECRET_LENGTH = 32


class RespondTokens:
    def __init__(self, secret: str):
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"LIFELINE_RESPOND_SECRET must be at least {MIN_SECRET_LENGTH} characters")
        self._key = secret.encode()

    def token(self, incident_id: str, alert_index: int) -> str:
        payload = f"{incident_id}:{alert_index}"
        return f"{_b64(payload.encode())}.{_b64(self._sign(payload))}"

    def verify(self, token: str) -> tuple[str, int] | None:
        """(incident id, alert index) if the token is genuine, otherwise None."""
        try:
            payload_part, signature_part = token.split(".")
            payload = _unb64(payload_part).decode()
            if not hmac.compare_digest(_unb64(signature_part), self._sign(payload)):
                return None
            incident_id, index_text = payload.rsplit(":", 1)
            return incident_id, int(index_text)
        except (ValueError, UnicodeDecodeError):
            return None

    def _sign(self, payload: str) -> bytes:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).digest()[:SIGNATURE_BYTES]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
