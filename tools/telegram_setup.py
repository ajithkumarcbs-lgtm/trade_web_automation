"""One-time Telegram login, then lists your groups/channels and their ids, for TELEGRAM_CHAT or channels.json.

    python -m tools.telegram_setup

Telegram sends a login code to your Telegram app; type it here. The session is saved in
data/telegram.session, so later runs don't ask again. Keep that file private: it is a login.
"""
import asyncio

from tradebot.config import TELEGRAM_FIELDS, load_settings
from tradebot.telegram_listener import make_client


async def main() -> None:
    settings = load_settings()
    settings.require(*TELEGRAM_FIELDS)
    client = make_client(settings)
    await client.start(phone=settings.tg_phone)
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} ({me.phone})\n")
    print(f"{'chat id':>18}  name")
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"{dialog.id:>18}  {dialog.name}")
    print("\nCopy the ids into the \"ids\" of channels.json (several channels), or one id into TELEGRAM_CHAT in .env."
          "\nAn id stays the same when a channel is renamed.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
