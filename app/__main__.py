"""Listen to messages/forwards and run Telegram delivery flows."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from html import escape
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import (
    ChatForwardsRestrictedError,
    FloodWaitError,
    UserAlreadyParticipantError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

from .channel_tracker import ChannelTracker
from .links import (
    StartLink,
    extract_bot_start_links_with_labels,
    extract_tme_links,
    find_start_links,
    join_target,
    parse_start_link,
)
from .progress import ProgressReporter
from .responses import is_message_blocked, is_transient_response
from .settings import (
    parse_csv_list,
    parse_int_setting,
    parse_requester_ids,
    parse_retry_delay,
)

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
    progress: ProgressReporter
    recipient: str | int
    sender_id: int
    sender_info: str


def load_env(path: str = ".env") -> None:
    """Load configuration from .env."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", maxsplit=1)
            os.environ.setdefault(key.strip(), value.strip())


async def safe_forward_messages(
    client: TelegramClient,
    recipient: str | int,
    messages: list[object],
    chunk_size: int = 5,
    delay_between_chunks: float = 1.5,
) -> int:
    """Forward messages in safe chunk sizes with automatic FloodWait handling."""
    sent = 0
    for i in range(0, len(messages), chunk_size):
        chunk = messages[i : i + chunk_size]
        for attempt in range(3):
            try:
                await client.forward_messages(recipient, chunk)
                sent += len(chunk)
                if i + chunk_size < len(messages):
                    await asyncio.sleep(delay_between_chunks)
                break
            except FloodWaitError as err:
                if err.seconds > 180:
                    logging.warning("FloodWait on forward too long (%ds). Skipping chunk.", err.seconds)
                    break
                logging.info("Sleeping %ds for ForwardMessages flood wait", err.seconds)
                await asyncio.sleep(err.seconds + 1)
            except ChatForwardsRestrictedError:
                logging.warning("Chat has restricted forwarding; could not forward chunk.")
                break
            except RPCError as err:
                logging.warning("RPC error forwarding chunk: %s", err)
                break
    return sent


async def join_urls(
    client: TelegramClient,
    urls: set[str],
    progress: ProgressReporter,
    tracker: ChannelTracker,
    pacing_delay: float,
) -> tuple[int, list[str]]:
    """Join supported URLs, editing the progress line in-place, and recording joins."""
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
        channel_name = f"https://t.me/+{value}" if kind == "invite" else f"@{value}"

        await progress.report(f"[➥] Joining {channel_name}")

        for attempt in range(2):
            try:
                if kind == "invite":
                    updates = await client(ImportChatInviteRequest(value))
                    chat = updates.chats[0] if getattr(updates, "chats", None) else value
                    await tracker.record_join(getattr(chat, "id", value))
                else:
                    channel = await client.get_input_entity(value)
                    await client(JoinChannelRequest(channel))
                    await tracker.record_join(getattr(channel, "channel_id", value))

                resolved += 1
                await progress.report(f"[✓] Joined {channel_name}", in_place=True)
                if pacing_delay > 0:
                    await asyncio.sleep(pacing_delay)
                break
            except UserAlreadyParticipantError:
                resolved += 1
                await progress.report(f"[✓] Already joined {channel_name}", in_place=True)
                break
            except FloodWaitError as error:
                if attempt == 1 or error.seconds > 180:
                    failures.append(f"{channel_name} (FloodWaitError: {error.seconds}s)")
                    await progress.report(f"[✗] Could not join {channel_name}", in_place=True)
                    break
                await progress.report(
                    f"[ⴵ] Rate limit: waiting {error.seconds}s before retrying {channel_name}",
                    in_place=True,
                )
                await asyncio.sleep(error.seconds)
                await progress.report(f"[➥] Re-joining {channel_name}")
            except (RPCError, TypeError) as error:
                logging.info("Could not join %s: %s", url, error.__class__.__name__)
                failures.append(f"{channel_name} ({error.__class__.__name__})")
                await progress.report(
                    f"[✗] Could not join {channel_name}: {error.__class__.__name__}",
                    in_place=True,
                )
                break
    return resolved, failures


def response_links(message: object) -> set[str]:
    """Collect Telegram URLs from text plus URL buttons on a message."""
    links = extract_tme_links(getattr(message, "raw_text", ""))
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            if button.url:
                links.add(button.url)
    return links


async def get_actionable_response(conversation: object, transient_texts: list[str]) -> object:
    """Skip short-lived spinner messages, waiting for the bot's next actual response."""
    for _ in range(MAX_TRANSIENT_RESPONSES):
        response = await conversation.get_response()
        if not is_transient_response(response, transient_texts):
            return response
        logging.info("Ignoring transient bot wait indicator")
    raise TimeoutError("Bot kept sending transient wait indicators")


async def collect_followups(
    conversation: object, first_response: object, transient_texts: list[str]
) -> list[object]:
    """Collect additional bot messages that arrive shortly after the first response."""
    messages = [first_response]
    while True:
        try:
            message = await asyncio.wait_for(
                conversation.get_response(), timeout=FOLLOWUP_WINDOW_SECONDS
            )
        except TimeoutError:
            return messages
        if not is_transient_response(message, transient_texts):
            messages.append(message)


async def run_bot_flow(
    client: TelegramClient,
    start: StartLink,
    retry_delay_seconds: float,
    progress: ProgressReporter,
    tracker: ChannelTracker,
    pacing_delay: float,
    transient_texts: list[str],
) -> tuple[list[object], int, list[str]]:
    """Run one bot's start/join/retry sequence and return its responses."""
    bot = await client.get_entity(start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await progress.report(f"[⌲] Sending /start to @{start.bot}")
        await conversation.send_message(f"/start {start.argument}")
        response = await get_actionable_response(conversation, transient_texts)
        await progress.report(f"[⎙] Received response from @{start.bot}")

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
                await progress.report(f"[⫶☰] Bot lists {channel_count} channel requirement(s)")

            resolved, join_failures = await join_urls(
                client, links, progress, tracker, pacing_delay
            )
            joined_total += resolved
            failures.extend(join_failures)

            if not resolved:
                break
            if retry_delay_seconds:
                await progress.report(f"[ⴵ] Waiting {retry_delay_seconds:g}s before retry")
                await asyncio.sleep(retry_delay_seconds)
            await progress.report(f"[↺⌲] Re-sending /start to @{start.bot}")
            await conversation.send_message(f"/start {start.argument}")
            response = await get_actionable_response(conversation, transient_texts)
            await progress.report(f"[⎙] Received updated response from @{start.bot}")

        messages = await collect_followups(conversation, response, transient_texts)

    return messages, joined_total, failures


async def deliver(
    client: TelegramClient,
    command: str,
    retry_delay_seconds: float,
    progress: ProgressReporter,
    recipient: str | int,
    tracker: ChannelTracker,
    pacing_delay: float,
    transient_texts: list[str],
    blocked_words: list[str],
) -> tuple[str, list[object]]:
    """Run bot flow, filter blocked texts, safely forward to recipient, and return status."""
    try:
        start = parse_start_link(command)
    except ValueError as error:
        return f"[✗] Invalid command: {error}", []

    current = start
    seen = {(start.bot, start.argument)}
    joined_total = 0
    failures: list[str] = []
    handoffs = 0
    responses: list[object] = []

    for _ in range(MAX_BOT_HANDOFFS):
        responses, joined, join_failures = await run_bot_flow(
            client, current, retry_delay_seconds, progress, tracker, pacing_delay, transient_texts
        )
        joined_total += joined
        failures.extend(join_failures)

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

    # Filter out blocked text-only messages and messages with forwarding restrictions
    deliverable_messages: list[object] = []
    await progress.report(f"[✶] Fetching {len(responses)} final message(s)")
    for response in responses:
        if getattr(response, "noforwards", False):
            await progress.report("[✗] Skipped protected message")
            continue
        if is_message_blocked(response, blocked_words):
            await progress.report("[✗] Skipped filtered promotional message")
            continue
        deliverable_messages.append(response)

    if not deliverable_messages:
        status = f"[✗] Cannot deliver: No valid content or protection enabled.{failure_note}"
        return status, []

    # Single clean line reporting delivery to requester
    await progress.report(f"[➴] Sending content to requester")

    # Safe chunked forward with automatic FloodWait recovery
    sent_count = await safe_forward_messages(client, recipient, deliverable_messages)
    if not sent_count:
        return f"[✗] Cannot deliver: Content could not be forwarded.{failure_note}", []

    status = (
        f"[✿] Done: forwarded {sent_count} message(s) from @{current.bot} "
        f"({joined_total} channel(s) resolved, {handoffs} handoff(s)).{failure_note}"
    )
    return status, deliverable_messages[:sent_count]


async def send_saved_report(
    client: TelegramClient,
    task: DeliveryTask,
    status: str,
    delivered_messages: list[object],
) -> None:
    """Send execution report to Saved Messages ('me') and safely forward delivered files."""
    user_status_flow = task.progress.get_rendered_text()

    report = (
        f"<b>[Task Report]</b>\n"
        f"<b>Status:</b> {escape(status)}\n\n"
        f"<b>[User Details]</b>\n"
        f"{task.sender_info}\n\n"
        f"<b>[Requested Link]</b>\n"
        f"<code>{escape(task.command)}</code>\n\n"
        f"<b>[Bot Process]</b>\n"
        f"<pre>{escape(user_status_flow)}</pre>\n\n"
        f"<b>[Sent Content Count]</b>: {len(delivered_messages)} message(s)"
    )
    try:
        await client.send_message("me", report, parse_mode="html")
        if delivered_messages:
            await safe_forward_messages(client, "me", delivered_messages)
    except Exception as e:
        logging.error("Failed to send task report to Saved Messages: %s", e)


async def process_tasks(
    queue: asyncio.Queue[DeliveryTask],
    client: TelegramClient,
    retry_delay_seconds: float,
    tracker: ChannelTracker,
    pacing_delay: float,
    transient_texts: list[str],
    blocked_words: list[str],
) -> None:
    """Process requests sequentially."""
    while True:
        task = await queue.get()
        delivered_messages: list[object] = []
        status = "Unknown"
        is_success = False
        try:
            await task.progress.report("[★] Started")
            status, delivered_messages = await deliver(
                client=client,
                command=task.command,
                retry_delay_seconds=retry_delay_seconds,
                progress=task.progress,
                recipient=task.recipient,
                tracker=tracker,
                pacing_delay=pacing_delay,
                transient_texts=transient_texts,
                blocked_words=blocked_words,
            )
            is_success = bool(delivered_messages)
            await task.progress.report(
                status,
                success=is_success,
                failed=not is_success,
                sent_count=len(delivered_messages),
            )
        except TimeoutError:
            status = "[✗] Timed out waiting for bot response"
            await task.progress.report(status, failed=True, sent_count=0)
        except (RPCError, TypeError) as error:
            logging.exception("Telegram error during delivery")
            status = f"[✗] Error: {error.__class__.__name__}"
            await task.progress.report(status, failed=True, sent_count=0)
        except Exception as error:
            logging.exception("Unexpected error during delivery")
            status = f"[✗] Error: {error.__class__.__name__}: {error}"
            await task.progress.report(status, failed=True, sent_count=0)
        finally:
            await send_saved_report(client, task, status, delivered_messages)
            queue.task_done()


async def enqueue_link(
    queue: asyncio.Queue[DeliveryTask],
    event: events.NewMessage.Event,
    target_link: str,
    recipient: str | int,
    user_info: str,
) -> None:
    """Send initial progress card and enqueue job."""
    initial_body = "<b>[⏳ Delivery in progress]</b>\n<pre>[★] Queued</pre>"
    message = await event.reply(initial_body, parse_mode="html")
    progress = ProgressReporter(message)
    try:
        queue.put_nowait(
            DeliveryTask(
                command=target_link,
                progress=progress,
                recipient=recipient,
                sender_id=event.sender_id,
                sender_info=user_info,
            )
        )
    except asyncio.QueueFull:
        await progress.report("[✗] Queue is full; please try again later", failed=True, sent_count=0)


async def main() -> None:
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    retry_delay_seconds = parse_retry_delay(os.environ.get("RETRY_DELAY_SECONDS"))
    pacing_delay = float(os.environ.get("JOIN_PACING_DELAY_SECONDS", "3"))
    max_channels = parse_int_setting(os.environ.get("MAX_JOINED_CHANNELS"), default=100)
    allowed_requester_ids = parse_requester_ids(os.environ.get("ALLOWED_REQUESTER_IDS"))

    default_transients = ["...", "please wait", "processing...", "⏳", "⌛"]
    transient_texts = parse_csv_list(os.environ.get("TRANSIENT_TEXTS"), default=default_transients)
    blocked_words = parse_csv_list(os.environ.get("BLOCKED_TEXT_WORDS"), default=[])

    client = TelegramClient(os.environ.get("SESSION_NAME", "telegram-automation"), api_id, api_hash)
    queue: asyncio.Queue[DeliveryTask] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    tracker = ChannelTracker()

    pending_choices: dict[str, dict] = {}

    @client.on(events.CallbackQuery)
    async def handle_choice(event: events.CallbackQuery.Event) -> None:
        data = event.data.decode("utf-8", errors="ignore")
        if not data.startswith("dl:"):
            return
        token = data.removeprefix("dl:")
        choice_data = pending_choices.pop(token, None)
        if not choice_data:
            await event.answer("This selection has expired.")
            return

        await event.answer("Starting download...")
        await event.delete()

        await enqueue_link(
            queue=queue,
            event=choice_data["event"],
            target_link=choice_data["url"],
            recipient=choice_data["recipient"],
            user_info=choice_data["user_info"],
        )

    @client.on(events.NewMessage)
    async def handle_message(event: events.NewMessage.Event) -> None:
        me = await client.get_me()

        if event.out:
            if event.chat_id != me.id:
                return
            recipient: str | int = "me"
        elif event.is_private and (not allowed_requester_ids or event.sender_id in allowed_requester_ids):
            recipient = event.sender_id
        else:
            return

        bot_links = extract_bot_start_links_with_labels(event.message)
        if not bot_links:
            return

        sender = await event.get_sender()
        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        username = f"@{sender.username}" if getattr(sender, "username", None) else "No Username"
        user_info = f"Name: {escape(first_name + ' ' + last_name).strip()} | {escape(username)} | ID: <code>{event.sender_id}</code>"

        if len(bot_links) == 1:
            await enqueue_link(queue, event, bot_links[0].url, recipient, user_info)
            return

        buttons = []
        for index, item in enumerate(bot_links, start=1):
            token = str(uuid.uuid4())[:8]
            pending_choices[token] = {
                "event": event,
                "url": item.url,
                "recipient": recipient,
                "user_info": user_info,
            }
            btn_title = f"{index}. {item.label}"
            buttons.append([Button.inline(btn_title, data=f"dl:{token}")])

        await event.reply("Multiple links detected. Choose one to start:", buttons=buttons)

    await client.start()
    worker = asyncio.create_task(
        process_tasks(
            queue=queue,
            client=client,
            retry_delay_seconds=retry_delay_seconds,
            tracker=tracker,
            pacing_delay=pacing_delay,
            transient_texts=transient_texts,
            blocked_words=blocked_words,
        )
    )
    cleanup_worker = asyncio.create_task(
        tracker.run_cleanup_loop(client=client, max_channels=max_channels)
    )

    logging.info("Ready. Forward or send any Telegram bot link.")
    try:
        await client.run_until_disconnected()
    finally:
        worker.cancel()
        cleanup_worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
            await cleanup_worker


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())