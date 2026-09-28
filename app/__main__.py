"""Listen to Saved Messages and run Telegram delivery flows."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from html import escape
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
from .progress import ProgressReporter, ProgressSink
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
    """One request waiting for the delivery worker."""

    command: str
    progress: ProgressSink
    recipient: str | int
    sender_id: int
    sender_info: str


def channel_link(url: str) -> str:
    """Make a safe, compact clickable channel label for the progress message."""
    return f'<a href="{escape(url, quote=True)}">[#] Open channel</a>'


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
                await progress.report(f"[>] Joining {channel_link(url)}", is_html=True)
                if kind == "invite":
                    await client(ImportChatInviteRequest(value))
                else:
                    channel = await client.get_input_entity(value)
                    await client(JoinChannelRequest(channel))
                resolved += 1
                await progress.report(f"[✓] Joined {channel_link(url)}", is_html=True)
                break
            except UserAlreadyParticipantError:
                resolved += 1
                await progress.report(f"[✓] Already in {channel_link(url)}", is_html=True)
                break
            except FloodWaitError as error:
                if attempt == 1:
                    failures.append(f"{url} (FloodWaitError after retry)")
                    await progress.report(f"[!] Could not join {channel_link(url)}", is_html=True)
                    break
                await progress.report(
                    f"[-] Rate limit: waiting {error.seconds}s before retrying {channel_link(url)}",
                    is_html=True,
                )
                await asyncio.sleep(error.seconds)
            except (RPCError, TypeError) as error:
                logging.info("Could not join %s: %s", url, error.__class__.__name__)
                failures.append(f"{url} ({error.__class__.__name__})")
                await progress.report(
                    f"[!] Could not join {channel_link(url)}: {error.__class__.__name__}", is_html=True
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
    client: TelegramClient,
    start: StartLink,
    retry_delay_seconds: float,
    progress: ProgressSink,
    bot_logs: list[str],
) -> tuple[list[object], int, list[str]]:
    """Run one bot's start/join/retry sequence and return its last response."""
    bot = await client.get_entity(start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await progress.report(f"[↑] Sending /start to @{start.bot}")
        await conversation.send_message(f"/start {start.argument}")
        response = await get_actionable_response(conversation)
        bot_logs.append(f"@{start.bot}: {getattr(response, 'raw_text', '')}")
        await progress.report(f"[↓] Received response from @{start.bot}")
        
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
                await progress.report(f"[*] Bot lists {channel_count} channel requirement(s)")
            resolved, join_failures = await join_urls(client, links, progress)
            joined_total += resolved
            failures.extend(join_failures)
            
            if not resolved:
                break
            if retry_delay_seconds:
                await progress.report(f"[-] Waiting {retry_delay_seconds:g}s before retry")
                await asyncio.sleep(retry_delay_seconds)
            await progress.report(f"[↺] Re-sending /start to @{start.bot}")
            await conversation.send_message(f"/start {start.argument}")
            response = await get_actionable_response(conversation)
            bot_logs.append(f"@{start.bot} (re-sent): {getattr(response, 'raw_text', '')}")
            await progress.report(f"[↓] Received updated response from @{start.bot}")

        messages = await collect_followups(conversation, response)
        for msg in messages[1:]:
            bot_logs.append(f"@{start.bot} (followup): {getattr(msg, 'raw_text', '')}")
            
    return messages, joined_total, failures


async def deliver(
    client: TelegramClient,
    command: str,
    retry_delay_seconds: float,
    progress: ProgressSink,
    recipient: str | int,
) -> tuple[str, list[str], list[str]]:
    """Run bot flows without bot restrictions and return delivery summaries."""
    bot_logs: list[str] = []
    delivered_contents: list[str] = []
    
    try:
        start = parse_start_link(command)
    except ValueError as error:
        return f"Invalid command: {error}", bot_logs, delivered_contents

    current = start
    seen = {(start.bot, start.argument)}
    joined_total = 0
    failures: list[str] = []
    handoffs = 0
    responses: list[object] = []

    for _ in range(MAX_BOT_HANDOFFS):
        responses, joined, join_failures = await run_bot_flow(
            client, current, retry_delay_seconds, progress, bot_logs
        )
        joined_total += joined
        failures.extend(join_failures)
        
        # Follow handoffs to any returned bot
        next_start = next(
            (
                link
                for link in find_start_links(
                    set().union(*(response_links(r) for r in responses))
                )
                if (link.bot, link.argument) not in seen
            ),
            None,
        )
        if next_start is None:
            break
        current = next_start
        seen.add((current.bot, current.argument))
        handoffs += 1
        await progress.report(f"[→] Following handoff to @{current.bot}")

    failure_note = f" Could not join: {'; '.join(failures)}." if failures else ""
    sent = 0
    await progress.report(f"[*] Fetching {len(responses)} final message(s)")
    for response in responses:
        if getattr(response, "noforwards", False):
            await progress.report("[!] Skipped protected message")
            continue
        try:
            await progress.report("[↑] Sending content to requester")
            await client.forward_messages(recipient, response)
            sent += 1
            
            content_desc = (
                f"Message (ID: {getattr(response, 'id', 'N/A')})"
                + (f" Text: {getattr(response, 'raw_text', '')[:100]}..." if getattr(response, 'raw_text', '') else "")
                + (" [Media]" if getattr(response, "media", None) else "")
            )
            delivered_contents.append(content_desc)
        except ChatForwardsRestrictedError:
            await progress.report("[!] Skipped protected message")

    if not sent:
        status = f"Cannot deliver: @{current.bot} has forwarding/copying protection enabled.{failure_note}"
        return status, bot_logs, delivered_contents

    status = (
        f"[✓] Done: forwarded {sent} message(s) from @{current.bot} "
        f"({joined_total} channel(s) resolved, {handoffs} handoff(s)).{failure_note}"
    )
    return status, bot_logs, delivered_contents


async def send_saved_report(
    client: TelegramClient,
    task: DeliveryTask,
    status: str,
    bot_logs: list[str],
    delivered_contents: list[str],
) -> None:
    """Send execution report to Saved Messages ('me')."""
    bot_proc = "\n".join(f"• {log.strip()}" for log in bot_logs) if bot_logs else "None"
    sent_repr = "\n".join(f"• {c}" for c in delivered_contents) if delivered_contents else "None (No content forwarded)"

    report = (
        f"<b>[Task Report]</b>\n"
        f"<b>Status:</b> {escape(status)}\n\n"
        f"<b>[User Details]</b>\n"
        f"{task.sender_info}\n\n"
        f"<b>[Requested Link]</b>\n"
        f"<code>{escape(task.command)}</code>\n\n"
        f"<b>[Bot Process]</b>\n"
        f"{escape(bot_proc)}\n\n"
        f"<b>[Sent Content]</b>\n"
        f"{escape(sent_repr)}"
    )
    try:
        await client.send_message("me", report, parse_mode="html")
    except Exception as e:
        logging.error("Failed to send task report to Saved Messages: %s", e)


async def process_tasks(
    queue: asyncio.Queue[DeliveryTask],
    client: TelegramClient,
    retry_delay_seconds: float,
) -> None:
    """Process delivery requests serially so account actions cannot overlap."""
    while True:
        task = await queue.get()
        bot_logs: list[str] = []
        delivered: list[str] = []
        status = "Unknown"
        try:
            await task.progress.report("[>] Started")
            status, bot_logs, delivered = await deliver(
                client,
                task.command,
                retry_delay_seconds,
                task.progress,
                task.recipient,
            )
            await task.progress.report(status)
        except TimeoutError:
            status = "Timed out waiting for bot response"
            await task.progress.report(f"[✗] {status}", failed=True)
        except (RPCError, TypeError) as error:
            logging.exception("Telegram error while handling /get")
            status = f"Error: {error.__class__.__name__}"
            await task.progress.report(f"[✗] {status}", failed=True)
        except Exception as error:
            logging.exception("Unexpected error while handling /get")
            status = f"Unexpected Error: {error.__class__.__name__}: {error}"
            await task.progress.report(f"[✗] {status}", failed=True)
        finally:
            await send_saved_report(client, task, status, bot_logs, delivered)
            queue.task_done()


async def main() -> None:
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    retry_delay_seconds = parse_retry_delay(os.environ.get("RETRY_DELAY_SECONDS"))
    allowed_requester_ids = parse_requester_ids(os.environ.get("ALLOWED_REQUESTER_IDS"))
    
    client = TelegramClient(os.environ.get("SESSION_NAME", "telegram-automation"), api_id, api_hash)
    queue: asyncio.Queue[DeliveryTask] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)

    @client.on(events.NewMessage(pattern=r"^/get\s+"))
    async def handle_get(event: events.NewMessage.Event) -> None:
        me = await client.get_me()
        sender = await event.get_sender()
        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        username = f"@{sender.username}" if getattr(sender, "username", None) else "No Username"
        user_info = f"Name: {escape(first_name + ' ' + last_name).strip()} | {escape(username)} | ID: <code>{event.sender_id}</code>"

        if event.out:
            if event.chat_id != me.id:
                return
            message = await event.reply(
                "[-] <b>Delivery queued</b>\n<i>Waiting to start…</i>", parse_mode="html"
            )
            progress: ProgressSink = ProgressReporter(message)
            recipient: str | int = "me"
        elif event.is_private and (not allowed_requester_ids or event.sender_id in allowed_requester_ids):
            requester_message = await event.reply(
                "[-] <b>Delivery queued</b>\n<i>Waiting to start…</i>", parse_mode="html"
            )
            progress = ProgressReporter(requester_message)
            recipient = event.sender_id
        else:
            return
            
        try:
            queue.put_nowait(
                DeliveryTask(
                    command=event.raw_text.removeprefix("/get ").strip(),
                    progress=progress,
                    recipient=recipient,
                    sender_id=event.sender_id,
                    sender_info=user_info,
                )
            )
        except asyncio.QueueFull:
            await progress.report("[✗] Queue is full; please try again later", failed=True)

    await client.start()
    worker = asyncio.create_task(process_tasks(queue, client, retry_delay_seconds))
    logging.info("Ready. Send /get <bot start link>.")
    try:
        await client.run_until_disconnected()
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())