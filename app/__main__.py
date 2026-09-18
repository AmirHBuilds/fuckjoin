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
from telethon.errors.rpcerrorlist import (
    ChatForwardsRestrictedError,
    FloodWaitError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .links import StartLink, extract_tme_links, find_start_links, join_target, parse_start_link
from .progress import MirroredProgressReporter, ProgressReporter, ProgressSink
from .responses import is_transient_response
from .settings import parse_requester_ids, parse_retry_delay

RESPONSE_TIMEOUT_SECONDS = 30
MAX_START_ATTEMPTS = 5
MAX_BOT_HANDOFFS = 5
MAX_TRANSIENT_RESPONSES = 10
FOLLOWUP_WINDOW_SECONDS = 2
MAX_QUEUE_SIZE = 100


@dataclass
class DeliveryTask:
    """One Saved Messages request waiting for the single delivery worker."""

    command: str
    progress: ProgressSink
    recipient: str | int


def channel_link(url: str) -> str:
    """Make a safe, compact clickable channel label for the progress message."""
    from html import escape

    return f'<a href="{escape(url, quote=True)}">[open channel]</a>'


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
    client: TelegramClient, urls: set[str], progress: ProgressSink
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
        for attempt in range(2):
            try:
                await progress.report(f"[JOIN] Joining {channel_link(url)}", is_html=True)
                if kind == "invite":
                    await client(ImportChatInviteRequest(value))
                else:
                    channel = await client.get_input_entity(value)
                    await client(JoinChannelRequest(channel))
                resolved += 1
                await progress.report(f"[OK] Joined {channel_link(url)}", is_html=True)
                break
            except UserAlreadyParticipantError:
                # The requirement is already satisfied; retrying /start can progress the flow.
                resolved += 1
                await progress.report(f"[OK] Already in {channel_link(url)}", is_html=True)
                break
            except FloodWaitError as error:
                if attempt == 1:
                    failures.append(f"{url} (FloodWaitError after retry)")
                    await progress.report(f"[WARN] Could not join {channel_link(url)}", is_html=True)
                    break
                await progress.report(
                    f"[WAIT] Telegram rate limit: waiting {error.seconds}s before retrying "
                    f"{channel_link(url)}",
                    is_html=True,
                )
                await asyncio.sleep(error.seconds)
            except (RPCError, TypeError) as error:
                logging.info("Could not join %s: %s", url, error.__class__.__name__)
                failures.append(f"{url} ({error.__class__.__name__})")
                await progress.report(
                    f"[WARN] Could not join {channel_link(url)}: {error.__class__.__name__}", is_html=True
                )
                break
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


async def collect_followups(conversation: object, first_response: object) -> list[object]:
    """Collect additional bot messages that arrive shortly after the first response."""
    messages = [first_response]
    while True:
        try:
            message = await asyncio.wait_for(
                conversation.get_response(), timeout=FOLLOWUP_WINDOW_SECONDS
            )
        except TimeoutError:
            return messages
        if not is_transient_response(message):
            messages.append(message)


async def run_bot_flow(
    client: TelegramClient, start: StartLink, retry_delay_seconds: float, progress: ProgressSink
) -> tuple[object, int, list[str]]:
    """Run one bot's start/join/retry sequence and return its last response."""
    bot = await client.get_entity(start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await progress.report(f"[SEND] Sending /start to @{start.bot}")
        await conversation.send_message(f"/start {start.argument}")
        response = await get_actionable_response(conversation)
        await progress.report(f"[RECEIVED] Response from @{start.bot}")
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
                await progress.report(f"[CHECK] Bot lists {channel_count} channel requirement(s)")
            resolved, join_failures = await join_urls(client, links, progress)
            joined_total += resolved
            failures.extend(join_failures)
            # Only retry after a join (or an already-member result) changes the account state.
            if not resolved:
                break
            if retry_delay_seconds:
                await progress.report(f"[WAIT] Waiting {retry_delay_seconds:g} second(s) before retry")
                await asyncio.sleep(retry_delay_seconds)
            await progress.report(f"[RETRY] Re-sending /start to @{start.bot}")
            await conversation.send_message(f"/start {start.argument}")
            response = await get_actionable_response(conversation)
            await progress.report(f"[RECEIVED] Updated response from @{start.bot}")

        messages = await collect_followups(conversation, response)
    return messages, joined_total, failures


async def deliver(
    client: TelegramClient,
    command: str,
    allowed_bots: set[str],
    retry_delay_seconds: float,
    progress: ProgressSink,
    recipient: str | int,
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
        responses, joined, join_failures = await run_bot_flow(
            client, current, retry_delay_seconds, progress
        )
        joined_total += joined
        failures.extend(join_failures)
        next_start = next(
            (
                link
                for link in find_start_links(
                    set().union(*(response_links(response) for response in responses))
                )
                if link.bot in allowed_bots and (link.bot, link.argument) not in seen
            ),
            None,
        )
        if next_start is None:
            break
        current = next_start
        seen.add((current.bot, current.argument))
        handoffs += 1
        await progress.report(f"[NEXT] Following handoff to @{current.bot}")

    failure_note = f" Could not join: {'; '.join(failures)}." if failures else ""
    sent = 0
    await progress.report(f"[FETCH] Fetching {len(responses)} final message(s)")
    for response in responses:
        if getattr(response, "noforwards", False):
            await progress.report("[WARN] Skipped protected message")
            continue
        try:
            await progress.report("[DELIVER] Sending content to requester")
            await client.forward_messages(recipient, response)
            sent += 1
        except ChatForwardsRestrictedError:
            await progress.report("[WARN] Skipped protected message")
    if not sent:
        return f"Cannot deliver: @{current.bot} has forwarding/copying protection enabled.{failure_note}"

    status = f"[DONE] Forwarded {sent} message(s) from @{current.bot} ({joined_total} channel(s) resolved, {handoffs} handoff(s))."
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
            await task.progress.report("[START] Started")
            status = await deliver(
                client,
                task.command,
                allowed_bots,
                retry_delay_seconds,
                task.progress,
                task.recipient,
            )
            await task.progress.report(status)
        except TimeoutError:
            await task.progress.report("Timed out waiting for the bot response", failed=True)
        except (RPCError, TypeError) as error:
            logging.exception("Telegram error while handling /get")
            await task.progress.report(f"{error.__class__.__name__}", failed=True)
        except Exception as error:
            logging.exception("Unexpected error while handling /get")
            await task.progress.report(f"{error.__class__.__name__}: {error}", failed=True)
        finally:
            queue.task_done()


async def main() -> None:
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    retry_delay_seconds = parse_retry_delay(os.environ.get("RETRY_DELAY_SECONDS"))
    allowed_requester_ids = parse_requester_ids(os.environ.get("ALLOWED_REQUESTER_IDS"))
    allowed_bots = {item.strip().lstrip("@").lower() for item in os.environ["ALLOWED_BOTS"].split(",") if item.strip()}
    if not allowed_bots:
        raise ValueError("ALLOWED_BOTS must contain at least one approved bot username")
    client = TelegramClient(os.environ.get("SESSION_NAME", "telegram-automation"), api_id, api_hash)
    queue: asyncio.Queue[DeliveryTask] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)

    @client.on(events.NewMessage(pattern=r"^/get\s+"))
    async def handle_get(event: events.NewMessage.Event) -> None:
        me = await client.get_me()
        if event.out:
            if event.chat_id != me.id:
                return
            message = await event.reply(
                "[QUEUED] <b>Delivery queued</b>\n<i>Waiting to start…</i>", parse_mode="html"
            )
            progress: ProgressSink = ProgressReporter(message)
            recipient: str | int = "me"
        elif event.is_private and event.sender_id in allowed_requester_ids:
            requester_message = await event.reply(
                "[QUEUED] <b>Delivery queued</b>\n<i>Waiting to start…</i>", parse_mode="html"
            )
            owner_message = await client.send_message(
                "me", f"[QUEUED] Remote request from user ID {event.sender_id}"
            )
            progress = MirroredProgressReporter(
                (ProgressReporter(requester_message), ProgressReporter(owner_message))
            )
            recipient = event.sender_id
        else:
            return
        try:
            queue.put_nowait(DeliveryTask(event.raw_text.removeprefix("/get ").strip(), progress, recipient))
        except asyncio.QueueFull:
            await progress.report("Queue is full; please try again later", failed=True)

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
