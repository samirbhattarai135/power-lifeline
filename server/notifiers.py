"""Ways to deliver a Notification. Console for demos without credentials,
ntfy for free phone push notifications, Twilio for real SMS and voice calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
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



class NtfyNotifier:
    """Phone push via ntfy (https://ntfy.sh): free, no account. Every alert goes to one
    topic that the demo phones subscribe to in the ntfy app. Anyone who knows the topic
    name can read it, so the name is the secret: keep it long and random."""

    MIN_TOPIC_LENGTH = 16

    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        if len(topic) < self.MIN_TOPIC_LENGTH:
            raise ValueError(f"ntfy topic must be at least {self.MIN_TOPIC_LENGTH} random characters")
        self._topic = topic
        self._server = server.rstrip("/")

    def send(self, notification: Notification) -> None:
        contact = notification.contact
        self._post({
            "topic": self._topic,
            "title": f"For {contact.name} ({contact.role})",
            "message": notification.message,
            "priority": 5,  # "urgent": long vibration, can break through Do Not Disturb
            "tags": ["rotating_light"],
        })

    def _post(self, payload: dict) -> None:
        request = urllib.request.Request(self._server, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                log.info("ntfy push for %s: HTTP %s", payload["title"], response.status)
        except urllib.error.HTTPError as error:
            log.error("ntfy push failed: HTTP %s %s", error.code, error.read()[:300])
        except urllib.error.URLError as error:
            log.error("ntfy push failed: %s", error.reason)

WHATSAPP_PREFIX = "whatsapp:"


class TwilioNotifier:
    """Sends SMS and voice calls. If `from_number` is a WhatsApp sender
    (e.g. Twilio's sandbox, "whatsapp:+14155238886"), every alert goes out as a
    WhatsApp message instead, calls included: WhatsApp senders cannot place calls."""

    API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/{resource}.json"

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self._sid = account_sid
        self._from = from_number
        self._is_whatsapp = from_number.startswith(WHATSAPP_PREFIX)
        credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
        self._auth_header = f"Basic {credentials}"

    def send(self, notification: Notification) -> None:
        contact = notification.contact
        if self._is_whatsapp:
            self._post("Messages", {"To": WHATSAPP_PREFIX + contact.phone, "From": self._from, "Body": notification.message})
        elif contact.channel is Channel.CALL:
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
