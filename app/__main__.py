"""Listen to Saved Messages and run approved Telegram delivery flows."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .links import extract_tme_links, join_target, parse_start_link

RESPONSE_TIMEOUT_SECONDS = 30


def load_env(path: str = ".env") -> None:
    """Load simple KEY=VALUE configuration without logging its secret values."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", maxsplit=1)
            os.environ.setdefault(key.strip(), value.strip())


async def join_urls(client: TelegramClient, urls: set[str]) -> int:
    """Join supported Telegram channel URLs and return the number joined."""
    joined = 0
    for url in urls:
        target = join_target(url)
        if target is None:
            continue
        kind, value = target
        try:
            if kind == "invite":
                await client(ImportChatInviteRequest(value))
            else:
                channel = await client.get_input_entity(value)
                await client(JoinChannelRequest(channel))
            joined += 1
        except RPCError as error:
            logging.info("Could not join %s: %s", url, error.__class__.__name__)
    return joined


def response_links(message: object) -> set[str]:
    """Collect Telegram URLs from text plus URL buttons on a Telethon message."""
    links = extract_tme_links(getattr(message, "raw_text", ""))
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if button.url:
                links.add(button.url)
    return links


async def deliver(client: TelegramClient, command: str, allowed_bots: set[str]) -> str:
    """Run one permitted /get command and forward the resulting bot response."""
    try:
        start = parse_start_link(command)
    except ValueError as error:
        return f"Invalid command: {error}"
    if start.bot not in allowed_bots:
        return f"Blocked: @{start.bot} is not in ALLOWED_BOTS."

    bot = await client.get_entity(start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await conversation.send_message(f"/start {start.argument}")
        first_response = await conversation.get_response()
        join_count = await join_urls(client, response_links(first_response))
        if join_count:
            await conversation.send_message(f"/start {start.argument}")
            response = await conversation.get_response()
        else:
            response = first_response
    await client.forward_messages("me", response)
    return f"Done: forwarded response from @{start.bot} ({join_count} channel(s) joined)."


async def main() -> None:
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    allowed_bots = {item.strip().lstrip("@").lower() for item in os.environ["ALLOWED_BOTS"].split(",") if item.strip()}
    if not allowed_bots:
        raise ValueError("ALLOWED_BOTS must contain at least one approved bot username")
    client = TelegramClient(os.environ.get("SESSION_NAME", "telegram-automation"), api_id, api_hash)
    lock = asyncio.Lock()

    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"^/get\s+"))
    async def handle_get(event: events.NewMessage.Event) -> None:
        async with lock:
            status = await deliver(client, event.raw_text.removeprefix("/get ").strip(), allowed_bots)
            await event.reply(status)

    await client.start()
    logging.info("Ready. Send /get <approved bot start link> to Saved Messages.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
