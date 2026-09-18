"""Twilio: proving a webhook call came from Twilio, answering it, and sending texts.

No Twilio library: its webhook signature is an HMAC anyone can check, its answer
is a few lines of XML, and sending is one authenticated form POST.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import mimetypes
from pathlib import Path
from typing import Callable, Mapping
from xml.sax.saxutils import escape

import requests

from .config import Settings

API = "https://api.twilio.com/2010-04-01"

#: Twilio error codes worth explaining in plain words.
HINTS = {
    20003: "the Account SID or Auth Token is wrong; copy them again from the Twilio console",
    21211: "that is not a valid phone number",
    21408: "texting that country is not enabled on the account (Messaging > Settings > Geo permissions)",
    21606: "the TWILIO_FROM number cannot send texts; pick an SMS-capable number",
    21608: "a trial account only texts phone numbers verified in the console (Phone Numbers > "
           "Verified Caller IDs): add that phone there, up to five",
    21610: "the farmer replied STOP; Twilio will not deliver to them until they reply START",
    21614: "that number cannot receive texts (a landline?)",
    30007: "the carrier filtered the text as spam; check the A2P 10DLC registration",
    30034: "the number is not registered for A2P 10DLC yet; US carriers block unregistered texts",
}


class TwilioError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def signature(url: str, params: Mapping[str, str], token: str) -> str:
    """Twilio's X-Twilio-Signature for a form POST to ``url``."""
    data = url + "".join(key + params[key] for key in sorted(params))
    digest = hmac.new(token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_signature(url: str, params: Mapping[str, str], token: str, header: str | None) -> bool:
    return bool(header) and hmac.compare_digest(signature(url, params, token), header)


def twiml(replies: list[str]) -> str:
    """The XML that tells Twilio to answer with these texts (none is fine)."""
    body = "".join(f"<Message>{escape(reply)}</Message>" for reply in replies)
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def _fail(response: requests.Response, what: str) -> TwilioError:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = payload.get("code")
    hint = HINTS.get(code, payload.get("message") or response.text[:200])
    return TwilioError(f"Twilio refused to {what} (HTTP {response.status_code}, code {code}): "
                       f"{hint}", code)


def send(settings: Settings, to: str, body: str, *, media: list[str] | None = None,
         post: Callable[..., requests.Response] | None = None) -> str:
    """Send one text; returns Twilio's message id.

    Raises:
        TwilioError: with Twilio's code and what it means.
    """
    if not settings.twilio_ready:
        raise TwilioError("Twilio is not set up: put TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
                          "TWILIO_FROM in farm_data/sms/sms.env")
    data = {"To": to, "Body": body}
    if settings.twilio_service:
        data["MessagingServiceSid"] = settings.twilio_service
    else:
        data["From"] = settings.twilio_from
    if settings.public_url.startswith("https://"):
        data["StatusCallback"] = settings.link("sms/status")
        # Twilio fetches a picture itself, so only a public link can carry one.
        pictures = [url for url in media or () if url.startswith("https://")]
        if pictures:
            data["MediaUrl"] = pictures
    try:
        response = (post or requests.post)(
            f"{API}/Accounts/{settings.twilio_sid}/Messages.json", data=data,
            auth=(settings.twilio_sid, settings.twilio_token), timeout=20,
        )
    except requests.RequestException as exc:
        raise TwilioError(f"could not reach Twilio: {exc}") from exc
    if response.status_code >= 400:
        raise _fail(response, "send the text")
    return response.json()["sid"]


def fetch_media(settings: Settings, url: str, folder: Path, stem: str, *,
                get: Callable[..., requests.Response] | None = None) -> Path:
    """Save a photo sent by text. Twilio asks for the account's keys to hand it over."""
    auth = (settings.twilio_sid, settings.twilio_token) if settings.twilio_ready else None
    try:
        response = (get or requests.get)(url, auth=auth, timeout=30)
    except requests.RequestException as exc:
        raise TwilioError(f"could not download the photo: {exc}") from exc
    if response.status_code >= 400:
        raise _fail(response, "hand over the photo")
    kind = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}{mimetypes.guess_extension(kind) or '.bin'}"
    path.write_bytes(response.content)
    return path


def point_number_at(settings: Settings, url: str, *,
                    get: Callable[..., requests.Response] | None = None,
                    post: Callable[..., requests.Response] | None = None) -> str:
    """Set the number's "A message comes in" webhook to ``url``; returns the number.

    A quick tunnel gets a new address every time it starts, and a webhook left
    on the old one drops every text, so the live launcher calls this each start.
    Only TWILIO_FROM numbers: a Messaging Service keeps its own webhook, set once
    in the console.
    """
    if not settings.twilio_ready:
        raise TwilioError("Twilio is not set up: put TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and "
                          "TWILIO_FROM in sms.env")
    if not settings.twilio_from:
        raise TwilioError("a Messaging Service keeps its own webhook: set it in the Twilio "
                          f"console to {url}")
    auth = (settings.twilio_sid, settings.twilio_token)
    base = f"{API}/Accounts/{settings.twilio_sid}/IncomingPhoneNumbers"
    try:
        found = (get or requests.get)(f"{base}.json", params={"PhoneNumber": settings.twilio_from},
                                      auth=auth, timeout=20)
    except requests.RequestException as exc:
        raise TwilioError(f"could not reach Twilio: {exc}") from exc
    if found.status_code >= 400:
        raise _fail(found, "look up the number")
    numbers = found.json().get("incoming_phone_numbers") or []
    if not numbers:
        raise TwilioError(f"{settings.twilio_from} is not a number on this Twilio account; "
                          "TWILIO_FROM must be the number you bought, like +18885550123")
    try:
        done = (post or requests.post)(f"{base}/{numbers[0]['sid']}.json",
                                       data={"SmsUrl": url, "SmsMethod": "POST"},
                                       auth=auth, timeout=20)
    except requests.RequestException as exc:
        raise TwilioError(f"could not reach Twilio: {exc}") from exc
    if done.status_code >= 400:
        raise _fail(done, "set the number's webhook")
    return numbers[0].get("phone_number") or settings.twilio_from
