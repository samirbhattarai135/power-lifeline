"""Ways to deliver a Notification. Console for demos without credentials,
Twilio for real SMS and voice calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

from domain import Channel, Notification

log = logging.getLogger("lifeline.notify")


def masked(phone: str) -> str:
    return f"***{phone[-4:]}"


class ConsoleNotifier:
    """Development stand-in for SMS: the log line IS the delivered message, so it
    deliberately includes the full text and family link. Never use it in production."""

    def send(self, notification: Notification) -> None:
        c = notification.contact
        log.info("[%s -> %s %s (%s)] %s", c.channel.value.upper(), c.role, c.name, c.phone, notification.message)


class TwilioNotifier:
    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self._sid = account_sid
        self._from = from_number
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self._auth_header = f"Basic {credentials}"

    def send(self, notification: Notification) -> None:
        contact = notification.contact
        if contact.channel is Channel.CALL:
            # Say the alert twice so a groggy listener catches it.
            spoken = escape(notification.message.split(" Reply OK")[0])  # links and SMS instructions make no sense aloud
            twiml = f'<Response><Say voice="alice">{spoken}</Say><Pause length="1"/><Say voice="alice">{spoken}</Say></Response>'
            self._post("Calls", {"To": contact.phone, "From": self._from, "Twiml": twiml})
        else:
            self._post("Messages", {"To": contact.phone, "From": self._from, "Body": notification.message})

    def _post(self, resource: str, fields: dict[str, str]) -> None:
        request = urllib.request.Request(
            self.API.format(sid=self._sid, resource=resource),
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Authorization": self._auth_header},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                log.info("twilio %s to %s: HTTP %s", resource, masked(fields["To"]), response.status)
        except urllib.error.HTTPError as error:
            log.error("twilio %s to %s failed: HTTP %s %s", resource, masked(fields["To"]), error.code, error.read()[:300])
        except urllib.error.URLError as error:
            log.error("twilio %s to %s failed: %s", resource, masked(fields["To"]), error.reason)


def is_valid_twilio_signature(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio signs webhooks with HMAC-SHA1 over the URL plus sorted POST params."""
    payload = url + "".join(key + params[key] for key in sorted(params))
    expected = base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)
