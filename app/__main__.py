"""Listen to Saved Messages and run approved Telegram delivery flows."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .links import StartLink, extract_tme_links, find_start_links, join_target, parse_start_link
from .progress import ProgressReporter
from .responses import is_transient_response
from .settings import parse_retry_delay

RESPONSE_TIMEOUT_SECONDS = 30
MAX_START_ATTEMPTS = 5
MAX_BOT_HANDOFFS = 5
MAX_TRANSIENT_RESPONSES = 10


@dataclass
class DeliveryTask:
    """One Saved Messages request waiting for the single delivery worker."""

    command: str
    progress: ProgressReporter


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


async def join_urls(
    client: TelegramClient, urls: set[str], progress: ProgressReporter
) -> tuple[int, list[str]]:
    """Join supported URLs, returning resolved count and failures for useful status logs."""
    resolved = 0
    failures: list[str] = []
    for url in urls:
        # A bot deep link is a handoff, not a public channel to join.
        try:
            parse_start_link(url)
            continue
        except ValueError:
            pass
        target = join_target(url)
        if target is None:
            continue
        kind, value = target
        try:
            await progress.report(f"Joining channel: {url}")
            if kind == "invite":
                await client(ImportChatInviteRequest(value))
            else:
                channel = await client.get_input_entity(value)
                await client(JoinChannelRequest(channel))
            resolved += 1
            await progress.report(f"Channel resolved: {url}")
        except UserAlreadyParticipantError:
            # The requirement is already satisfied; retrying /start can progress the flow.
            resolved += 1
            await progress.report(f"Already in channel: {url}")
        except (RPCError, TypeError) as error:
            logging.info("Could not join %s: %s", url, error.__class__.__name__)
            failures.append(f"{url} ({error.__class__.__name__})")
            await progress.report(f"Could not join {url}: {error.__class__.__name__}")
    return resolved, failures


def response_links(message: object) -> set[str]:
    """Collect Telegram URLs from text plus URL buttons on a Telethon message."""
    links = extract_tme_links(getattr(message, "raw_text", ""))
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if button.url:
                links.add(button.url)
    return links


async def get_actionable_response(conversation: object) -> object:
    """Skip short-lived spinner messages, waiting for the bot's next actual response."""
    for _ in range(MAX_TRANSIENT_RESPONSES):
        response = await conversation.get_response()
        if not is_transient_response(response):
            return response
        logging.info("Ignoring transient bot wait indicator")
    raise TimeoutError("Bot kept sending transient wait indicators")


async def run_bot_flow(
    client: TelegramClient, start: StartLink, retry_delay_seconds: float, progress: ProgressReporter
) -> tuple[object, int, list[str]]:
    """Run one bot's start/join/retry sequence and return its last response."""
    bot = await client.get_entity(start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await progress.report(f"Sending /start to @{start.bot}")
        await conversation.send_message(f"/start {start.argument}")
        response = await get_actionable_response(conversation)
        await progress.report(f"Received response from @{start.bot}")
        joined_total = 0
        failures: list[str] = []
        seen_links: set[str] = set()
        for _ in range(MAX_START_ATTEMPTS):
            links = response_links(response) - seen_links
            seen_links.update(links)
            channel_count = sum(
                1
                for url in links
                if join_target(url) is not None
                and not any(link.bot for link in find_start_links({url}))
            )
            if channel_count:
                await progress.report(f"Bot lists {channel_count} channel requirement(s)")
            resolved, join_failures = await join_urls(client, links, progress)
            joined_total += resolved
            failures.extend(join_failures)
            # Only retry after a join (or an already-member result) changes the account state.
            if not resolved:
                break
            if retry_delay_seconds:
                await progress.report(f"Waiting {retry_delay_seconds:g} second(s) before retry")
                await asyncio.sleep(retry_delay_seconds)
            await progress.report(f"Re-sending /start to @{start.bot}")
            await conversation.send_message(f"/start {start.argument}")
            response = await get_actionable_response(conversation)
            await progress.report(f"Received updated response from @{start.bot}")

    return response, joined_total, failures


async def deliver(
    client: TelegramClient,
    command: str,
    allowed_bots: set[str],
    retry_delay_seconds: float,
    progress: ProgressReporter,
) -> str:
    """Run permitted bot flows, following explicitly allowed bot deep-link handoffs."""
    try:
        start = parse_start_link(command)
    except ValueError as error:
        return f"Invalid command: {error}"
    if start.bot not in allowed_bots:
        return f"Blocked: @{start.bot} is not in ALLOWED_BOTS."

    current = start
    seen = {(start.bot, start.argument)}
    joined_total = 0
    failures: list[str] = []
    handoffs = 0
    for _ in range(MAX_BOT_HANDOFFS):
        response, joined, join_failures = await run_bot_flow(
            client, current, retry_delay_seconds, progress
        )
        joined_total += joined
        failures.extend(join_failures)
        next_start = next(
            (
                link
                for link in find_start_links(response_links(response))
                if link.bot in allowed_bots and (link.bot, link.argument) not in seen
            ),
            None,
        )
        if next_start is None:
            break
        current = next_start
        seen.add((current.bot, current.argument))
        handoffs += 1
        await progress.report(f"Following handoff to @{current.bot}")

    failure_note = f" Could not join: {'; '.join(failures)}." if failures else ""
    if getattr(response, "noforwards", False):
        return f"Cannot deliver: @{current.bot} has forwarding/copying protection enabled.{failure_note}"
    try:
        await progress.report("Sending final content to Saved Messages")
        await client.forward_messages("me", response)
    except ChatForwardsRestrictedError:
        return f"Cannot deliver: @{current.bot} has forwarding/copying protection enabled.{failure_note}"

    status = f"Done: forwarded response from @{current.bot} ({joined_total} channel(s) resolved, {handoffs} handoff(s))."
    return status + failure_note


async def process_tasks(
    queue: asyncio.Queue[DeliveryTask],
    client: TelegramClient,
    allowed_bots: set[str],
    retry_delay_seconds: float,
) -> None:
    """Process delivery requests serially so account actions cannot overlap."""
    while True:
        task = await queue.get()
        try:
            await task.progress.report("Started")
            status = await deliver(
                client, task.command, allowed_bots, retry_delay_seconds, task.progress
            )
            await task.progress.report(status)
        except TimeoutError:
            await task.progress.report("Failed: timed out waiting for the bot response")
        except (RPCError, TypeError) as error:
            logging.exception("Telegram error while handling /get")
            await task.progress.report(f"Failed: {error.__class__.__name__}")
        except Exception as error:
            logging.exception("Unexpected error while handling /get")
            await task.progress.report(f"Failed: {error.__class__.__name__}: {error}")
        finally:
            queue.task_done()


async def main() -> None:
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    retry_delay_seconds = parse_retry_delay(os.environ.get("RETRY_DELAY_SECONDS"))
    allowed_bots = {item.strip().lstrip("@").lower() for item in os.environ["ALLOWED_BOTS"].split(",") if item.strip()}
    if not allowed_bots:
        raise ValueError("ALLOWED_BOTS must contain at least one approved bot username")
    client = TelegramClient(os.environ.get("SESSION_NAME", "telegram-automation"), api_id, api_hash)
    queue: asyncio.Queue[DeliveryTask] = asyncio.Queue()

    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"^/get\s+"))
    async def handle_get(event: events.NewMessage.Event) -> None:
        message = await event.reply("Queued: waiting to start...")
        await queue.put(
            DeliveryTask(event.raw_text.removeprefix("/get ").strip(), ProgressReporter(message))
        )

    await client.start()
    worker = asyncio.create_task(process_tasks(queue, client, allowed_bots, retry_delay_seconds))
    logging.info("Ready. Send /get <approved bot start link> to Saved Messages.")
    try:
        await client.run_until_disconnected()
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
