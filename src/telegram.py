"""Telegram: farmers message a bot instead of texting a number.

A stand-in for SMS while a real number is not ready. The bot's token comes from
@BotFather in Telegram (TELEGRAM_BOT_TOKEN in sms.env). The server asks Telegram
for new messages itself (long polling), so no webhook or public address is needed
for the chat; links in the answers (map, uploads, WHY) still need one.

Telegram gives a chat number, not a phone number. A farmer here is kept under
``+0`` and that chat number: no real phone number starts with a 0 country code, so
the two can never mix, and everything else treats it like any other farmer.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import requests

from .config import Settings

if TYPE_CHECKING:
    from .web import App

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
PREFIX = "+0"
POLL_S = 30
LIMIT = 4096        # characters in one Telegram message
PHOTO_MAX = 10 * 1024 * 1024    # the most Telegram takes for a photo sent as a file


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def phone_for(chat_id: int) -> str:
    return f"{PREFIX}{chat_id}"


def is_telegram(phone: str) -> bool:
    return phone.startswith(PREFIX)


def chat_of(phone: str) -> int:
    return int(phone[len(PREFIX):])


def _call(settings: Settings, method: str, data: dict, *, timeout: float = 20,
          post: Callable[..., requests.Response] | None = None,
          files: dict | None = None) -> dict:
    if not settings.telegram_token:
        raise TelegramError("Telegram is not set up: put TELEGRAM_BOT_TOKEN in sms.env")
    try:
        extra = {"files": files} if files else {}
        response = (post or requests.post)(f"{API}/bot{settings.telegram_token}/{method}",
                                           data=data, timeout=timeout, **extra)
    except requests.RequestException as exc:
        # The token is in the address; never let it reach a log.
        raise TelegramError(f"could not reach Telegram ({type(exc).__name__})") from None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not payload.get("ok"):
        code = payload.get("error_code") or response.status_code
        hint = {401: "the bot token is wrong; copy it again from @BotFather",
                403: "the person blocked the bot"}.get(code, payload.get("description", ""))
        raise TelegramError(f"Telegram refused to {method} (code {code}): {hint}", code)
    return payload["result"]


def send(settings: Settings, phone: str, body: str, *, media: list[str] | None = None,
         post: Callable[..., requests.Response] | None = None) -> str:
    """Send one message (and its pictures); returns Telegram's id of the last one."""
    chat = chat_of(phone)
    sent = None
    for start in range(0, max(len(body), 1), LIMIT):
        sent = _call(settings, "sendMessage",
                     {"chat_id": chat, "text": body[start:start + LIMIT],
                      "disable_web_page_preview": "true"}, post=post)
    # Telegram fetches a picture itself, so only a public link can carry one.
    for url in media or ():
        local = _our_picture(settings, url)
        if local is not None:
            # Handed over directly: fetching it back through a slow tunnel can take
            # longer than Telegram waits for a link.
            sent = _call(settings, "sendPhoto", {"chat_id": chat}, post=post, timeout=60,
                         files={"photo": (local.name, local.read_bytes())})
        elif url.startswith("https://"):
            sent = _call(settings, "sendPhoto", {"chat_id": chat, "photo": url}, post=post)
    return str(sent["message_id"]) if sent else ""


def _our_picture(settings: Settings, url: str) -> Path | None:
    """The file behind one of this server's own picture links (/p/<token>), if any."""
    prefix = settings.link("p/")
    if not url.startswith(prefix):
        return None
    from . import store

    with store.session(settings.db_path) as conn:
        link = store.get_link(conn, url[len(prefix):], "picture")
    if link is None:
        return None
    path = Path(json.loads(link["meta"] or "{}").get("path", ""))
    return path if path.is_file() and path.stat().st_size <= PHOTO_MAX else None


def _save_file(settings: Settings, file_id: str, folder: Path, stem: str) -> Path:
    found = _call(settings, "getFile", {"file_id": file_id})
    url = f"{API}/file/bot{settings.telegram_token}/{found['file_path']}"
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.RequestException:
        raise TelegramError("could not download the photo from Telegram") from None
    suffix = Path(found["file_path"]).suffix or mimetypes.guess_extension(
        response.headers.get("Content-Type", "image/jpeg").split(";")[0]) or ".jpg"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}{suffix}"
    path.write_bytes(response.content)
    return path


def inbound(settings: Settings, update: dict):
    """One Telegram update as the bot's :class:`Inbound`, or None to ignore it."""
    from .bot import Inbound, Media

    message = update.get("message")
    if not message or message.get("chat", {}).get("type") != "private":
        return None                          # groups and edits are not farmers texting
    phone = phone_for(message["chat"]["id"])
    body = (message.get("text") or message.get("caption") or "").strip()
    if body.startswith("/start"):
        body = "hola"                        # what Telegram sends when someone opens the bot
    media = []
    files = []
    if message.get("photo"):
        files.append((message["photo"][-1]["file_id"], "image/jpeg"))   # the largest size
    document = message.get("document") or {}
    if document.get("mime_type", "").startswith("image/"):
        files.append((document["file_id"], document["mime_type"]))
    for index, (file_id, kind) in enumerate(files):
        stem = f"{datetime.now():%Y%m%d-%H%M%S}_tg{update['update_id']}_{index}"
        try:
            path = _save_file(settings, file_id, settings.media_dir / phone.lstrip("+"), stem)
        except TelegramError as exc:
            log.warning("photo from %s not saved: %s", phone, exc)
            continue
        media.append(Media(str(path), kind))
    place = message.get("location") or {}
    return Inbound(phone, body, media, place.get("latitude"), place.get("longitude"),
                   "telegram")


def listen(app: "App", *, stop: threading.Event | None = None) -> None:
    """Take new messages from Telegram and answer them, until ``stop`` is set."""
    stop = stop or threading.Event()
    offset = None
    while not stop.is_set():
        try:
            data = {"timeout": POLL_S, "allowed_updates": '["message"]'}
            if offset is not None:
                data["offset"] = offset
            updates = _call(app.settings, "getUpdates", data, timeout=POLL_S + 10)
        except TelegramError as exc:
            log.warning("Telegram: %s", exc)
            if exc.code == 401:
                return
            stop.wait(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                message = inbound(app.settings, update)
                if message is not None:
                    app.receive(message, provider_id=f"tg{update['update_id']}")
            except Exception:
                log.exception("a Telegram message could not be handled")


def start_listening(app: "App") -> threading.Thread:
    thread = threading.Thread(target=listen, args=(app,), name="telegram", daemon=True)
    thread.start()
    return thread
