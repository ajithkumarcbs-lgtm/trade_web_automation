"""Reads one Telegram group with your own account and hands each new message to a callback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.tl.custom import Message

from .config import DATA_DIR, Settings

log = logging.getLogger(__name__)

SESSION_PATH = DATA_DIR / "telegram"  # Telethon stores the login in telegram.session


@dataclass
class IncomingMessage:
    id: int
    date: datetime  # UTC
    text: str
    image: bytes | None


def make_client(settings: Settings) -> TelegramClient:
    return TelegramClient(str(SESSION_PATH), settings.tg_api_id, settings.tg_api_hash)


async def to_incoming(msg: Message, download_image: bool = True) -> IncomingMessage:
    image = None
    is_image = msg.photo or (msg.document and (msg.file.mime_type or "").startswith("image/"))
    if is_image and download_image:
        image = await msg.download_media(file=bytes)
    return IncomingMessage(id=msg.id, date=msg.date, text=msg.raw_text or "", image=image)


async def resolve_chat(client: TelegramClient, chat: str | int):
    await client.get_dialogs()  # fills the entity cache so numeric group ids resolve
    return await client.get_entity(chat)


async def listen(
    settings: Settings,
    on_message: Callable[[IncomingMessage], Awaitable[None]],
    on_history: Callable[[list[IncomingMessage]], None],
    history_limit: int,
) -> None:
    """on_history gets the latest messages (oldest first, no images) once at start, as context."""
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    chat = await resolve_chat(client, settings.tg_chat)
    log.info("Listening to Telegram chat: %s", getattr(chat, "title", chat.id))

    recent = [await to_incoming(m, download_image=False) async for m in client.iter_messages(chat, limit=history_limit)]
    on_history(recent[::-1])

    @client.on(events.NewMessage(chats=chat))
    async def _handler(event: events.NewMessage.Event) -> None:
        await on_message(await to_incoming(event.message))

    await client.run_until_disconnected()
