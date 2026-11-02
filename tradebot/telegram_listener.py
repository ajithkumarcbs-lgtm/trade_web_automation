"""Reads the followed Telegram chats with your own account and hands each new message to the pipeline.

Chats come from channels.json (or the single TELEGRAM_CHAT). Every 10 minutes the bot also looks for chats
you joined whose title matches a channel's title_regex (the tipster's next tournament channel) and follows
them. Commands you send to your own Saved Messages ("/status", "/pause", "/yes 3", ...) go to on_command.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from telethon import TelegramClient, events, utils
from telethon.tl.custom import Message

from .config import DATA_DIR, Settings

log = logging.getLogger(__name__)

SESSION_PATH = DATA_DIR / "telegram"  # Telethon stores the login in telegram.session
DISCOVER_EVERY_S = 600  # how often to look for newly joined chats that match channels.json
SPEECH_FILES = {"voice": "voice.ogg", "video_note": "video.mp4", "audio": "audio.mp3"}


@dataclass
class IncomingMessage:
    id: int
    date: datetime  # UTC
    text: str
    image: bytes | None
    reply_to: str = ""  # text of the message this one replies to, e.g. the entry a "Bookset" answers
    chat_id: int = 0
    chat_title: str = ""
    voice: bool = False  # text includes the transcript of a voice / video note
    forwarded: bool = False  # forwarded from elsewhere (members' testimonials, reposts)

    @property
    def caption(self) -> str:
        """The message's own text, without the transcript of its voice note."""
        cut = self.text.rfind(VOICE_MARK) if self.voice else -1
        return self.text[:cut].strip() if cut >= 0 else self.text


VOICE_MARK = "(voice note) "  # starts the transcript in the text


def make_client(settings: Settings) -> TelegramClient:
    return TelegramClient(str(SESSION_PATH), settings.tg_api_id, settings.tg_api_hash)


def speech_kind(msg: Message) -> str | None:
    for kind in SPEECH_FILES:
        if getattr(msg, kind, None):
            return kind
    return None


async def to_incoming(msg: Message, download_image: bool = True, transcriber=None,
                      chat_title: str = "") -> IncomingMessage:
    """download_image=False for context-only history (no downloads, no transcription)."""
    image = None
    is_image = msg.photo or (msg.document and (msg.file.mime_type or "").startswith("image/"))
    if is_image and download_image:
        try:
            image = await msg.download_media(file=bytes)
        except Exception:  # the tip is still read from its text
            log.warning("could not download the screenshot of msg %s", msg.id, exc_info=True)
    reply_to = ""
    if msg.is_reply and download_image:
        try:
            replied = await msg.get_reply_message()
            if replied:
                reply_to = f"[{replied.date:%H:%M}] {replied.raw_text or '(media)'}"
        except Exception:  # context only: never lose the tip because of it
            log.warning("could not read the message replied to", exc_info=True)
    text, voice = msg.raw_text or "", False
    kind = speech_kind(msg)
    if kind and transcriber is not None and download_image:
        try:
            spoken = await transcriber(await msg.download_media(file=bytes), SPEECH_FILES[kind])
            text, voice = f"{text}\n{VOICE_MARK}{spoken}".strip(), True
            log.info("msg %s voice note: %s", msg.id, spoken[:120])
        except Exception:  # the message is still logged, without its words
            log.warning("could not transcribe the voice note of msg %s", msg.id, exc_info=True)
            text = f"{text}\n(voice note that could not be transcribed)".strip()
    return IncomingMessage(id=msg.id, date=msg.date, text=text, image=image, reply_to=reply_to,
                           chat_id=msg.chat_id or 0, chat_title=chat_title, voice=voice,
                           forwarded=msg.fwd_from is not None)


async def resolve_chat(client: TelegramClient, chat: str | int):
    await client.get_dialogs()  # fills the entity cache so numeric group ids resolve
    return await client.get_entity(chat)


async def _resolve_refs(client: TelegramClient, channels) -> None:
    """@usernames in channels.json / TELEGRAM_CHAT -> chat ids."""
    for channel in channels.all():
        for ref in list(channel.refs):
            try:
                entity = await client.get_entity(ref)
            except (ValueError, TypeError) as exc:
                raise SystemExit(f"Telegram chat '{ref}' of channel '{channel.name}' not found: {exc}") from exc
            channels.follow(utils.get_peer_id(entity), channel.name)
            channel.refs.remove(ref)


async def _discover(client: TelegramClient, channels, store, titles: dict[int, str],
                    on_new_chat: Callable[..., Awaitable[None]] | None) -> None:
    """Follows chats you joined whose title matches a channel's title_regex; refreshes chat titles."""
    async for dialog in client.iter_dialogs():
        if not (dialog.is_group or dialog.is_channel):
            continue
        titles[dialog.id] = dialog.name or ""
        if dialog.id in channels.all_ids():
            continue
        channel = channels.by_title(dialog.name or "")
        if channel is None:
            continue
        channels.follow(dialog.id, channel.name)
        store.follow(dialog.id, channel.name, dialog.name or "")
        log.info("Now following '%s' (%s) as %s: its title matches", dialog.name, dialog.id, channel.name)
        if on_new_chat:
            await on_new_chat(channel, dialog.id, dialog.name or "")


async def listen(
    settings: Settings,
    channels,
    store,
    on_message: Callable[[int, Awaitable[IncomingMessage]], Awaitable[None]],
    on_history: Callable[[list[IncomingMessage]], None],
    history_limit: int,
    on_connected: Callable[[TelegramClient], None] | None = None,
    on_command: Callable[[str], Awaitable[None]] | None = None,
    on_new_chat: Callable[..., Awaitable[None]] | None = None,
    transcriber=None,
    on_deleted: Callable[[int, list[int]], Awaitable[None]] | None = None,
    on_edited: Callable[[int, int, str], Awaitable[None]] | None = None,
) -> None:
    """on_message(chat id, message) gets each new message the moment it arrives, as an awaitable that downloads
    its screenshot / transcribes its voice note, so the pipeline keeps each chat's messages in order.
    on_history gets each followed chat's latest messages (oldest first, no images) once at start, as context.
    on_connected gets the logged-in client, e.g. so alerts can be sent through it.
    on_deleted(chat id, message ids) is told when a followed channel deletes messages (tipsters hiding losses).
    on_edited(chat id, message id, text) is told when one changes a message."""
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    me = await client.get_me()
    if on_connected:
        on_connected(client)
    await client.get_dialogs()
    for chat_id, name, title in store.followed():
        try:
            channels.follow(chat_id, name)
        except ValueError:
            log.warning("chat %s (%s) was followed as '%s', which channels.json no longer has", chat_id, title, name)
    await _resolve_refs(client, channels)
    titles: dict[int, str] = {}
    await _discover(client, channels, store, titles, on_new_chat)

    for channel in channels.all():
        for chat_id in sorted(channel.ids):
            try:
                entity = await client.get_entity(chat_id)
                recent = [await to_incoming(m, download_image=False, chat_title=titles.get(chat_id, ""))
                          async for m in client.iter_messages(entity, limit=history_limit)]
                on_history(recent[::-1])
                log.info("Following '%s' (%s) as %s%s", titles.get(chat_id, getattr(entity, "title", chat_id)),
                         chat_id, channel.name, "" if channel.profile.trade else " (watch only)")
            except Exception:
                log.warning("could not open chat %s of channel %s", chat_id, channel.name, exc_info=True)

    @client.on(events.MessageDeleted())
    async def _deleted(event: events.MessageDeleted.Event) -> None:
        # Telegram names the chat only for channels / supergroups, which is what tipsters use
        if on_deleted and event.chat_id and channels.for_chat(event.chat_id):
            await on_deleted(event.chat_id, list(event.deleted_ids))

    @client.on(events.MessageEdited())
    async def _edited(event: events.MessageEdited.Event) -> None:
        if on_edited and channels.for_chat(event.chat_id):
            await on_edited(event.chat_id, event.message.id, event.message.raw_text or "")

    @client.on(events.NewMessage())
    async def _handler(event: events.NewMessage.Event) -> None:
        if event.out and event.chat_id == me.id:  # your own Saved Messages: commands only
            text = (event.message.raw_text or "").strip()
            if text.startswith("/") and on_command:
                await on_command(text)
            return
        channel = channels.for_chat(event.chat_id)
        if channel is None:
            return
        voice = transcriber if channel.profile.voice else None

        async def read() -> IncomingMessage:
            title = titles.get(event.chat_id, "")
            if not title:
                chat = await event.get_chat()
                title = titles[event.chat_id] = getattr(chat, "title", "") or ""
            return await to_incoming(event.message, transcriber=voice, chat_title=title)

        # Handed over before anything is awaited: Telegram events run side by side, and a voice note (seconds
        # to transcribe) must not end up behind the "Limit 2" that came after it.
        await on_message(event.chat_id, read())

    async def rediscover() -> None:
        while True:
            await asyncio.sleep(DISCOVER_EVERY_S)
            try:
                await _discover(client, channels, store, titles, on_new_chat)
            except Exception:
                log.warning("looking for new chats failed", exc_info=True)

    finder = asyncio.get_running_loop().create_task(rediscover())
    try:
        await client.run_until_disconnected()
    finally:
        finder.cancel()
