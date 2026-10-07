"""One-time Telegram login, then lists your groups/channels so you can pick TELEGRAM_CHAT.

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
    print(f"{'TELEGRAM_CHAT id':>18}  name")
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"{dialog.id:>18}  {dialog.name}")
    print("\nCopy the id of the tips group into TELEGRAM_CHAT in .env")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
