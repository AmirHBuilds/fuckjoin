"""Listen to messages/forwards and run Telegram delivery flows with admin control and cancellation."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from telethon import Button, TelegramClient, events
from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import (
    ChatForwardsRestrictedError,
    FloodWaitError,
    UserAlreadyParticipantError,
    YouBlockedUserError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.contacts import UnblockRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek

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
from .user_manager import UserManager

RESPONSE_TIMEOUT_SECONDS = 30
MAX_START_ATTEMPTS = 5
MAX_BOT_HANDOFFS = 5
MAX_TRANSIENT_RESPONSES = 10
FOLLOWUP_WINDOW_SECONDS = 2
MAX_QUEUE_SIZE = 100
CLEANUP_DELAY_SECONDS = 1.5


class DeliveryCancelledError(Exception):
    """Raised when an in-progress delivery task is cancelled by the user."""


@dataclass
class DeliveryTask:
    """One request waiting for or running in the delivery worker."""

    task_id: str
    command: str
    progress: ProgressReporter
    recipient: str | int
    sender_id: int
    sender_info: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


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


async def interruptible_sleep(duration: float, cancel_event: asyncio.Event) -> None:
    """Sleep for duration, but return immediately if cancel_event is set."""
    if cancel_event.is_set():
        raise DeliveryCancelledError()
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=duration)
        raise DeliveryCancelledError()
    except asyncio.TimeoutError:
        pass


async def safe_forward_messages(
    client: TelegramClient,
    recipient: str | int,
    messages: list[object],
    progress: ProgressReporter | None = None,
    cancel_event: asyncio.Event | None = None,
    chunk_size: int = 5,
    delay_between_chunks: float = 1.5,
) -> int:
    """Forward messages in safe chunk sizes with FloodWait and cancellation handling."""
    sent = 0
    valid_messages = [
        msg for msg in messages 
        if msg and (getattr(msg, "media", None) or getattr(msg, "raw_text", "").strip())
    ]
    if not valid_messages:
        return 0

    for i in range(0, len(valid_messages), chunk_size):
        if cancel_event and cancel_event.is_set():
            raise DeliveryCancelledError()

        chunk = valid_messages[i : i + chunk_size]
        for attempt in range(3):
            if cancel_event and cancel_event.is_set():
                raise DeliveryCancelledError()
            try:
                res = await client.forward_messages(recipient, chunk)
                if isinstance(res, list):
                    sent += len(res)
                elif res:
                    sent += len(chunk)
                if i + chunk_size < len(valid_messages):
                    if cancel_event:
                        await interruptible_sleep(delay_between_chunks, cancel_event)
                    else:
                        await asyncio.sleep(delay_between_chunks)
                break
            except FloodWaitError as err:
                msg = f"[ⴵ] Rate limit: waiting {err.seconds}s before retrying forward..."
                logging.warning(msg)
                if progress:
                    await progress.report(msg)

                if cancel_event:
                    await interruptible_sleep(err.seconds + 1, cancel_event)
                else:
                    await asyncio.sleep(err.seconds + 1)

                if progress:
                    await progress.report("[➴] Resuming content delivery...")
            except ChatForwardsRestrictedError:
                logging.warning("Chat has restricted forwarding; skipped chunk.")
                break
            except RPCError as err:
                logging.warning("RPC error forwarding chunk: %s", err)
                break
    return sent


async def resolve_invite_chat_id(client: TelegramClient, invite_hash: str) -> int | None:
    """Find the chat id behind an invite link once we are a member.

    Many force-subscribe bots use "request to join" links and auto-approve the request a
    moment later, so the import call returns no chat. Poll the invite until it reports us
    as a member.
    """
    for _ in range(4):
        await asyncio.sleep(1.5)
        try:
            info = await client(CheckChatInviteRequest(invite_hash))
        except RPCError:
            return None
        if isinstance(info, (ChatInviteAlready, ChatInvitePeek)) and getattr(info.chat, "id", None):
            return info.chat.id
    return None


async def join_urls(
    client: TelegramClient,
    urls: set[str],
    progress: ProgressReporter,
    tracker: ChannelTracker,
    pacing_delay: float,
    cancel_event: asyncio.Event,
) -> tuple[int, list[str]]:
    """Join supported URLs with cancellation checkpoints."""
    resolved = 0
    failures: list[str] = []
    for url in urls:
        if cancel_event.is_set():
            raise DeliveryCancelledError()

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
            if cancel_event.is_set():
                raise DeliveryCancelledError()
            try:
                if kind == "invite":
                    updates = await client(ImportChatInviteRequest(value))
                    chat = updates.chats[0] if getattr(updates, "chats", None) else None
                    chat_id = getattr(chat, "id", None)
                    if chat_id is None:
                        # Join-request link (usually auto-approved by the bot): resolve the id.
                        chat_id = await resolve_invite_chat_id(client, value)
                    # If it still can't be resolved, keep the invite hash; /cleanup retries.
                    await tracker.record_join(chat_id if chat_id is not None else value)
                else:
                    channel = await client.get_input_entity(value)
                    await client(JoinChannelRequest(channel))
                    await tracker.record_join(getattr(channel, "channel_id", value))

                resolved += 1
                await progress.report(f"[✓] Joined {channel_name}", in_place=True)
                if pacing_delay > 0:
                    await interruptible_sleep(pacing_delay, cancel_event)
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
                await interruptible_sleep(error.seconds, cancel_event)
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


async def get_actionable_response(
    conversation: object, transient_texts: list[str], cancel_event: asyncio.Event
) -> object:
    """Skip short-lived spinner messages with cancellation checks."""
    for _ in range(MAX_TRANSIENT_RESPONSES):
        if cancel_event.is_set():
            raise DeliveryCancelledError()
        response_task = asyncio.create_task(conversation.get_response())
        cancel_waiter = asyncio.create_task(cancel_event.wait())
        done, _ = await asyncio.wait([response_task, cancel_waiter], return_when=asyncio.FIRST_COMPLETED)

        if cancel_event.is_set():
            response_task.cancel()
            raise DeliveryCancelledError()

        cancel_waiter.cancel()
        response = response_task.result()

        if not is_transient_response(response, transient_texts):
            return response
        logging.info("Ignoring transient bot wait indicator")
    raise TimeoutError("Bot kept sending transient wait indicators")


async def collect_followups(
    conversation: object,
    first_response: object,
    transient_texts: list[str],
    cancel_event: asyncio.Event,
) -> list[object]:
    """Collect additional bot messages arriving shortly after first response."""
    messages = [first_response]
    while True:
        if cancel_event.is_set():
            raise DeliveryCancelledError()
        try:
            message = await asyncio.wait_for(
                conversation.get_response(), timeout=FOLLOWUP_WINDOW_SECONDS
            )
        except TimeoutError:
            return messages
        if not is_transient_response(message, transient_texts):
            messages.append(message)


async def send_to_bot(
    client: TelegramClient,
    conversation: object,
    bot: object,
    text: str,
    progress: ProgressReporter,
) -> None:
    """Send a message to a bot, automatically unblocking it first if it is blocked."""
    try:
        await conversation.send_message(text)
    except YouBlockedUserError:
        await progress.report(f"[⚿] @{getattr(bot, 'username', None) or bot.id} is blocked, unblocking")
        await client(UnblockRequest(bot))
        await progress.report("[✓] Unblocked", in_place=True)
        await conversation.send_message(text)


async def run_bot_flow(
    client: TelegramClient,
    start: StartLink,
    retry_delay_seconds: float,
    progress: ProgressReporter,
    tracker: ChannelTracker,
    pacing_delay: float,
    transient_texts: list[str],
    cancel_event: asyncio.Event,
) -> tuple[list[object], int, list[str]]:
    """Run one bot's start/join/retry sequence."""
    if cancel_event.is_set():
        raise DeliveryCancelledError()

    bot = await client.get_entity(start.bot)
    await tracker.record_bot(bot.id, getattr(bot, "username", None) or start.bot)
    async with client.conversation(bot, timeout=RESPONSE_TIMEOUT_SECONDS) as conversation:
        await progress.report(f"[⌲] Sending /start to @{start.bot}")
        await send_to_bot(client, conversation, bot, f"/start {start.argument}", progress)
        response = await get_actionable_response(conversation, transient_texts, cancel_event)
        await progress.report(f"[⎙] Received response from @{start.bot}")

        joined_total = 0
        failures: list[str] = []
        seen_links: set[str] = set()

        for _ in range(MAX_START_ATTEMPTS):
            if cancel_event.is_set():
                raise DeliveryCancelledError()

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
                client, links, progress, tracker, pacing_delay, cancel_event
            )
            joined_total += resolved
            failures.extend(join_failures)

            if not resolved:
                break
            if retry_delay_seconds:
                await progress.report(f"[ⴵ] Waiting {retry_delay_seconds:g}s before retry")
                await interruptible_sleep(retry_delay_seconds, cancel_event)
            await progress.report(f"[↺⌲] Re-sending /start to @{start.bot}")
            await send_to_bot(client, conversation, bot, f"/start {start.argument}", progress)
            response = await get_actionable_response(conversation, transient_texts, cancel_event)
            await progress.report(f"[⎙] Received updated response from @{start.bot}")

        messages = await collect_followups(conversation, response, transient_texts, cancel_event)

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
    cancel_event: asyncio.Event,
) -> tuple[str, list[object]]:
    """Fetch content and deliver to recipient."""
    if cancel_event.is_set():
        raise DeliveryCancelledError()

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
        if cancel_event.is_set():
            raise DeliveryCancelledError()
        responses, joined, join_failures = await run_bot_flow(
            client,
            current,
            retry_delay_seconds,
            progress,
            tracker,
            pacing_delay,
            transient_texts,
            cancel_event,
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

    if cancel_event.is_set():
        raise DeliveryCancelledError()

    failure_note = f" Could not join: {'; '.join(failures)}." if failures else ""

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

    try:
        saved_backup = await client.forward_messages("me", deliverable_messages)
        if not isinstance(saved_backup, list):
            saved_backup = [saved_backup]
    except Exception as e:
        logging.error("Failed to back up to Saved Messages: %s", e)
        saved_backup = deliverable_messages

    if cancel_event.is_set():
        raise DeliveryCancelledError()

    if recipient != "me":
        await progress.report("[➴] Sending content to requester")
        sent_count = await safe_forward_messages(
            client=client,
            recipient=recipient,
            messages=saved_backup,
            progress=progress,
            cancel_event=cancel_event,
        )
    else:
        sent_count = len(saved_backup)

    if not sent_count:
        return f"[✗] Cannot deliver: Content could not be forwarded.{failure_note}", []

    status = (
        f"[✿] Done: forwarded {sent_count} message(s) from @{current.bot} "
        f"({joined_total} channel(s) resolved, {handoffs} handoff(s)).{failure_note}"
    )
    return status, saved_backup[:sent_count]


async def send_saved_report(
    client: TelegramClient,
    task: DeliveryTask,
    status: str,
    delivered_messages: list[object],
) -> None:
    """Send execution report summary to Saved Messages ('me')."""
    user_status_flow = task.progress.get_rendered_text()
    if len(user_status_flow) > 3000:
        user_status_flow = user_status_flow[:3000] + "\n...[truncated]"

    count = len(delivered_messages)

    report = (
        f"<b>[Task Report]</b>\n"
        f"<b>Status:</b> {escape(status)}\n\n"
        f"<b>[User Details]</b>\n"
        f"{task.sender_info}\n\n"
        f"<b>[Requested Link]</b>\n"
        f"<code>{escape(task.command)}</code>\n\n"
        f"<b>[Bot Process]</b>\n"
        f"<pre>{escape(user_status_flow)}</pre>\n\n"
        f"<b>[Sent Content Count]</b>: {count} message(s)"
    )
    try:
        await client.send_message("me", report, parse_mode="html")
    except Exception:
        with suppress(Exception):
            plain_report = (
                f"[Task Report]\nStatus: {status}\n\n"
                f"User: {task.sender_info}\n"
                f"Link: {task.command}\n\n"
                f"[Bot Process]\n{user_status_flow}\n\n"
                f"Sent: {count} message(s)"
            )
            await client.send_message("me", plain_report)


async def process_tasks(
    queue: asyncio.Queue[DeliveryTask],
    client: TelegramClient,
    retry_delay_seconds: float,
    tracker: ChannelTracker,
    pacing_delay: float,
    transient_texts: list[str],
    blocked_words: list[str],
    active_tasks: dict[str, DeliveryTask],
) -> None:
    """Process requests sequentially."""
    while True:
        task = await queue.get()
        active_tasks[task.task_id] = task
        delivered_messages: list[object] = []
        status = "Unknown"
        try:
            if task.cancel_event.is_set():
                raise DeliveryCancelledError()

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
                cancel_event=task.cancel_event,
            )
            is_success = bool(delivered_messages)
            await task.progress.report(
                status,
                success=is_success,
                failed=not is_success,
                sent_count=len(delivered_messages),
            )
        except DeliveryCancelledError:
            status = "[⊘] Process was cancelled by user."
            await task.progress.report(status, cancelled=True, sent_count=0)
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
            active_tasks.pop(task.task_id, None)
            await send_saved_report(client, task, status, delivered_messages)
            queue.task_done()


async def enqueue_link(
    queue: asyncio.Queue[DeliveryTask],
    event: events.NewMessage.Event,
    target_link: str,
    recipient: str | int,
    user_info: str,
    active_tasks: dict[str, DeliveryTask],
) -> None:
    """Send initial progress card and enqueue job."""
    task_id = str(uuid.uuid4())[:8]
    initial_body = (
        "<b>[⏳ Delivery in progress]</b>\n"
        "<pre>[★] Queued</pre>\n\n"
        "💡 <i>Tip: Send <code>/cancel</code> or <code>cancel</code> to stop.</i>"
    )
    message = await event.reply(initial_body, parse_mode="html", buttons=Button.clear())

    progress = ProgressReporter(message=message, task_id=task_id)
    task = DeliveryTask(
        task_id=task_id,
        command=target_link,
        progress=progress,
        recipient=recipient,
        sender_id=event.sender_id,
        sender_info=user_info,
    )
    active_tasks[task_id] = task

    try:
        queue.put_nowait(task)
    except asyncio.QueueFull:
        active_tasks.pop(task_id, None)
        await progress.report(
            "[✗] Queue is full; please try again later", failed=True, sent_count=0
        )


async def main() -> None:
    logging.getLogger("telethon").setLevel(logging.INFO)
    load_env()
    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    retry_delay_seconds = parse_retry_delay(os.environ.get("RETRY_DELAY_SECONDS"))
    pacing_delay = float(os.environ.get("JOIN_PACING_DELAY_SECONDS", "3"))
    max_channels = parse_int_setting(os.environ.get("MAX_JOINED_CHANNELS"), default=100)

    # ADMINS: Has full control and can use /admin, /add_user, /state, etc.
    admins = parse_requester_ids(os.environ.get("ADMINS"))

    default_transients = ["...", "please wait", "processing...", "⏳", "⌛"]
    transient_texts = parse_csv_list(os.environ.get("TRANSIENT_TEXTS"), default=default_transients)
    blocked_words = parse_csv_list(os.environ.get("BLOCKED_TEXT_WORDS"), default=[])

    client = TelegramClient(
        os.environ.get("SESSION_NAME", "telegram-automation"),
        api_id,
        api_hash,
        flood_sleep_threshold=0,
    )
    queue: asyncio.Queue[DeliveryTask] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
    tracker = ChannelTracker()
    user_manager = UserManager()

    pending_user_choices: dict[int, dict[str, dict]] = {}
    active_tasks: dict[str, DeliveryTask] = {}

    @client.on(events.NewMessage)
    async def handle_message(event: events.NewMessage.Event) -> None:
        me = await client.get_me()

        is_self = event.out and event.chat_id == me.id
        sender_id = me.id if is_self else event.sender_id

        # Determine authorization
        allowed_users = await user_manager.get_allowed_users()
        is_admin = is_self or (sender_id in admins)
        is_allowed_user = sender_id in allowed_users

        # Reject unauthorized requests in private messages
        if not is_self:
            if not event.is_private or not (is_admin or is_allowed_user):
                return

        recipient: str | int = "me" if is_self else sender_id
        text = (event.raw_text or "").strip()
        lower_text = text.lower()

        # --- ADMIN-ONLY COMMANDS ---
        if is_admin:
            # /admin or admin command list
            if lower_text in {"/admin", "admin"}:
                admin_help = (
                    "<b>🛠️ Admin Dashboard</b>\n\n"
                    "<b>Commands:</b>\n"
                    "• <code>/state</code> - View system status and allowed users\n"
                    "• <code>/add_user &lt;id&gt;</code> - Grant bot access to a user\n"
                    "• <code>/remove_user &lt;id&gt;</code> - Revoke user access\n"
                    "• <code>/cleanup</code> - Leave all joined channels, delete and block all bots it used\n"
                    "• <code>/cancel</code> - Cancel your running download\n\n"
                    "<i>Blocked bots are unblocked automatically when you send a link that needs them.</i>\n"
                    "<i>Allowed users can only send bot links and cancel their tasks.</i>"
                )
                admin_menu = [
                    [Button.text("/state", resize=True), Button.text("/admin", resize=True)],
                    [Button.text("/cleanup", resize=True), Button.text("/cancel", resize=True)],
                ]
                await event.reply(admin_help, parse_mode="html", buttons=admin_menu)
                return

            # /state command
            if lower_text in {"/state", "state"}:
                current_allowed = sorted(list(allowed_users))
                users_list_str = (
                    "\n".join([f"  • <code>{uid}</code>" for uid in current_allowed])
                    if current_allowed
                    else "<i>None</i>"
                )
                admin_list_str = (
                    ", ".join([f"<code>{aid}</code>" for aid in admins])
                    if admins
                    else f"<code>{me.id}</code> (Session Owner)"
                )

                state_msg = (
                    "<b>📊 Automation State Report</b>\n\n"
                    f"<b>Admins:</b> {admin_list_str}\n"
                    f"<b>Active Worker Tasks:</b> {len(active_tasks)}\n"
                    f"<b>Pending Queue Size:</b> {queue.qsize()}/{MAX_QUEUE_SIZE}\n\n"
                    f"<b>Allowed Users ({len(current_allowed)}):</b>\n{users_list_str}"
                )
                await event.reply(state_msg, parse_mode="html")
                return

            # /cleanup
            if lower_text in {"/cleanup", "cleanup"}:
                if tracker.cleanup_lock.locked():
                    await event.reply("ℹ️ A cleanup is already in progress.")
                    return
                if active_tasks:
                    await event.reply(
                        "⚠️ Deliveries are running or queued. Wait for them to finish "
                        "or send <code>/cancel</code>, then try again.",
                        parse_mode="html",
                    )
                    return
                async with tracker.cleanup_lock:
                    n_channels = tracker.channel_count()
                    n_bots = tracker.bot_count()
                    if not n_channels and not n_bots:
                        await event.reply("ℹ️ Nothing to clean up.")
                        return
                    status_msg = await event.reply(
                        f"🧹 Cleaning up: <b>{n_channels}</b> channel(s), <b>{n_bots}</b> bot(s)...",
                        parse_mode="html",
                    )
                    res = await tracker.cleanup_all(client, delay=CLEANUP_DELAY_SECONDS)
                lines = [
                    "<b>🧹 Cleanup finished</b>",
                    f"• Channels left: <b>{res.channels_left}</b>"
                    + (f" ({res.channels_failed} failed)" if res.channels_failed else ""),
                    *(
                        [f"• Unresolvable entries dropped: <b>{res.channels_stale}</b> (not a member / invite expired)"]
                        if res.channels_stale
                        else []
                    ),
                    f"• Bots deleted &amp; blocked: <b>{res.bots_cleaned}</b>"
                    + (f" ({res.bots_failed} failed)" if res.bots_failed else ""),
                ]
                if res.rate_limited:
                    lines.append(
                        "⛔ Stopped by Telegram rate limit "
                        f"({res.channels_remaining} channel(s), {res.bots_remaining} bot(s) left). "
                        "Run <code>/cleanup</code> again later."
                    )
                result_text = "\n".join(lines)
                try:
                    await status_msg.edit(result_text, parse_mode="html")
                except Exception:
                    await event.reply(result_text, parse_mode="html")
                return

            # /add_user <user_id>
            if lower_text.startswith("/add_user ") or lower_text.startswith("add_user "):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    await event.reply("⚠️ Usage: <code>/add_user 123456789</code>", parse_mode="html")
                    return
                new_user_id = int(parts[1])
                added = await user_manager.add_user(new_user_id)
                if added:
                    await event.reply(f"✅ User <code>{new_user_id}</code> is now allowed to use the bot.", parse_mode="html")
                else:
                    await event.reply(f"ℹ️ User <code>{new_user_id}</code> was already in the allowed list.", parse_mode="html")
                return

            # /remove_user <user_id>
            if lower_text.startswith("/remove_user ") or lower_text.startswith("remove_user "):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    await event.reply("⚠️ Usage: <code>/remove_user 123456789</code>", parse_mode="html")
                    return
                remove_id = int(parts[1])
                removed = await user_manager.remove_user(remove_id)
                if removed:
                    await event.reply(f"🗑️ User <code>{remove_id}</code> has been removed.", parse_mode="html")
                else:
                    await event.reply(f"ℹ️ User <code>{remove_id}</code> is not in the allowed list.", parse_mode="html")
                return

        # --- USER & ADMIN COMMON COMMANDS ---

        # Cancellation
        if lower_text in {"/cancel", "cancel"}:
            cancelled_any = False
            for task in list(active_tasks.values()):
                if task.sender_id == sender_id or (is_self and recipient == "me"):
                    task.cancel_event.set()
                    cancelled_any = True

            pending_user_choices.pop(sender_id, None)

            if cancelled_any:
                await event.reply("[⊘] Cancellation signal sent. Halting operation...", buttons=Button.clear())
            else:
                await event.reply("No active tasks found to cancel.", buttons=Button.clear())
            return

        # Handle multiple choice answers
        if sender_id in pending_user_choices:
            user_choices = pending_user_choices[sender_id]
            matched_key = None

            if text in user_choices:
                matched_key = text
            else:
                for k, v in user_choices.items():
                    if text == v["label"] or text == f"{k}. {v['label']}":
                        matched_key = k
                        break

            if matched_key:
                chosen = user_choices[matched_key]
                del pending_user_choices[sender_id]
                await enqueue_link(
                    queue=queue,
                    event=event,
                    target_link=chosen["url"],
                    recipient=chosen["recipient"],
                    user_info=chosen["user_info"],
                    active_tasks=active_tasks,
                )
                return

        bot_links = extract_bot_start_links_with_labels(event.message)
        if not bot_links:
            return

        sender = await event.get_sender()
        first_name = getattr(sender, "first_name", "") or ""
        last_name = getattr(sender, "last_name", "") or ""
        username = f"@{sender.username}" if getattr(sender, "username", None) else "No Username"
        user_info = f"Name: {escape(first_name + ' ' + last_name).strip()} | {escape(username)} | ID: <code>{sender_id}</code>"

        if len(bot_links) == 1:
            await enqueue_link(queue, event, bot_links[0].url, recipient, user_info, active_tasks)
            return

        # Multi-link selection menu
        keyboard_buttons = []
        user_choices_map = {}
        prompt_lines = ["Multiple links detected. Tap an option or send its number:\n"]

        for index, item in enumerate(bot_links, start=1):
            key = str(index)
            label = item.label or f"Option {index}"
            user_choices_map[key] = {
                "url": item.url,
                "label": label,
                "recipient": recipient,
                "user_info": user_info,
            }
            button_text = f"{index}. {label}"
            keyboard_buttons.append([Button.text(button_text, resize=True, single_use=True)])
            prompt_lines.append(f"<b>{index}.</b> {escape(label)}")

        pending_user_choices[sender_id] = user_choices_map
        prompt_lines.append("\n<i>Send <code>/cancel</code> to abort.</i>")

        await event.reply(
            "\n".join(prompt_lines),
            parse_mode="html",
            buttons=keyboard_buttons,
        )

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
            active_tasks=active_tasks,
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