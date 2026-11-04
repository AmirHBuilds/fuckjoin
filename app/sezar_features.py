"""Opt-in, bounded Telegram channel-reaction helper for fuckjoin.

Never reacts unless AUTO_REACT_LAST_POSTS is enabled and a bot explicitly asks
for reactions, or the target channel is explicitly configured. Uses only the
signed-in account and respects FloodWait. No background bulk-engagement jobs.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable

from telethon.errors import RPCError
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.functions.messages import CheckChatInviteRequest, SendReactionRequest
from telethon.tl.types import ChatInviteAlready, ChatInvitePeek, ReactionEmoji

from .links import join_target

REACTION_WORDS = re.compile(
    r"(?i)(?:\blikes?\b|\breact(?:ion|ions)?\b|\bthumbs?\s*up\b|"
    r"لایک|ری\s*اکشن|واکنش|پسندیدن|بپسندید|پسند\s*کردن)"
)


def reaction_prompt(text: str) -> bool:
    return bool(REACTION_WORDS.search(text or ""))


def _match_target(target: tuple[str, str], configured: set[str]) -> bool:
    if not configured:
        return True
    kind, value = target
    options = {value.casefold(), f"@{value}".casefold()}
    if kind == "invite":
        options.update({f"+{value}".casefold(), f"https://t.me/+{value}".casefold()})
    else:
        options.add(f"https://t.me/{value}".casefold())
    return bool(options & configured)


async def _entity_from_link(client: object, target: tuple[str, str]) -> object | None:
    kind, value = target
    if kind == "invite":
        invite = await client(CheckChatInviteRequest(value))
        if isinstance(invite, (ChatInviteAlready, ChatInvitePeek)):
            return invite.chat
        return None  # Not yet accepted into private channel
    return await client.get_entity(value)


async def react_to_required_channel(
    client: object,
    channel_urls: Iterable[str],
    instructions: str,
    *,
    count: int,
    emoji: str,
    configured_channels: set[str],
    attempted: set[str],
    progress: object,
    cancel_event: object,
    sleep: Callable[[float, object], Awaitable[None]],
    pacing_seconds: float = 5.0,
) -> int:
    """React conservatively to required channels, never bypassing FloodWait.

    AUTO_REACT_MODE=required: a single prompted/explicitly configured channel.
    AUTO_REACT_MODE=all: up to five posts in every *joined* required channel,
        bounded by AUTO_REACT_MAX_PER_FLOW (defaults to 25).
    This can generate rate limits; no amount of pacing guarantees otherwise.
    """
    count = max(0, min(5, count))
    mode = os.environ.get("AUTO_REACT_MODE", "required").strip().casefold()
    if mode not in {"required", "all"}:
        mode = "required"
    if not count:
        return 0
    if mode != "all" and not configured_channels and not reaction_prompt(instructions):
        return 0
    try:
        max_total = max(1, min(50, int(os.environ.get("AUTO_REACT_MAX_PER_FLOW", "25"))))
    except ValueError:
        max_total = 25
    pacing_seconds = max(5.0, pacing_seconds)
    candidates: list[tuple[str, tuple[str, str]]] = []
    for url in sorted(set(channel_urls)):
        if url in attempted:
            continue
        target = join_target(url)
        if target is not None and (not configured_channels or _match_target(target, configured_channels)):
            candidates.append((url, target))
    if not candidates:
        return 0

    # Without all-mode, limit reactions to one channel in the whole bot flow.
    if mode != "all":
        if attempted:
            return 0
        candidates = candidates[:1]

    reacted_total = 0
    for url, target in candidates:
        if reacted_total >= max_total or cancel_event.is_set():
            break
        attempted.add(url)
        reacted_here = 0
        try:
            peer = await _entity_from_link(client, target)
            if peer is None:
                await progress.report("[!] Required channel is not yet accessible for reactions")
                continue
            async for msg in client.iter_messages(peer, limit=30):
                if cancel_event.is_set():
                    return reacted_total
                if reacted_here >= count or reacted_total >= max_total:
                    break
                if getattr(msg, 'action', None) or not getattr(msg, 'id', None):
                    continue
                try:
                    await client(SendReactionRequest(
                        peer=peer, msg_id=msg.id,
                        reaction=[ReactionEmoji(emoticon=emoji)],
                    ))
                except FloodWaitError as err:
                    await progress.report(f"[⏳] Telegram requires {err.seconds}s before another reaction")
                    # Observe the delay. Avoid a second immediate retry if the
                    # server imposes another limit on the same message.
                    await sleep(err.seconds + 1, cancel_event)
                    try:
                        await client(SendReactionRequest(
                            peer=peer, msg_id=msg.id,
                            reaction=[ReactionEmoji(emoticon=emoji)],
                        ))
                    except FloodWaitError:
                        raise  # Recovery handles this cooldown and retries original link.
                    except (RPCError, TypeError) as exc:
                        logging.warning("Reaction retry rejected on %s: %s", msg.id, type(exc).__name__)
                        break
                except (RPCError, TypeError) as exc:
                    logging.warning("Reaction rejected on %s: %s", msg.id, type(exc).__name__)
                    continue
                reacted_here += 1
                reacted_total += 1
                if reacted_here < count:
                    await sleep(pacing_seconds, cancel_event)
            await progress.report(f"[{emoji}] Reacted to {reacted_here}/{count} posts in {url}")
        except FloodWaitError:
            raise
        except RPCError as exc:
            logging.warning("Cannot access reaction channel %s: %s", url, type(exc).__name__)
        if mode == "all" and reacted_total < max_total and not cancel_event.is_set():
            await sleep(pacing_seconds, cancel_event)
    return reacted_total

VERIFY_WORDS = re.compile(
    r"(?i)(?:\bcheck\b|\bverify\b|\bconfirm\b|\bdone\b|\bcontinue\b|"
    r"بررسی|تأیید|تایید|چک\s*کردن|عضویت\s*کردم|✅)"
)


async def click_check_button(response: object, clicked: set[tuple[int, int, int]]) -> bool:
    """Click at most three explicit callback verification buttons in a flow.

    Never follows external URL buttons and never clicks payment/ad buttons.
    """
    if len(clicked) >= 3:
        return False
    for row_index, row in enumerate(getattr(response, 'buttons', None) or []):
        for column_index, button in enumerate(row):
            key = (getattr(response, 'id', 0), row_index, column_index)
            if key in clicked or getattr(button, 'data', None) is None:
                continue
            if not VERIFY_WORDS.search(getattr(button, 'text', '') or ''):
                continue
            clicked.add(key)
            try:
                await response.click(row_index, column_index)
            except FloodWaitError:
                raise  # Respect cooldown in delivery recovery.
            except (TypeError, ValueError, RPCError) as exc:
                logging.warning("Verification callback failed: %s: %s", type(exc).__name__, exc)
                continue
            return True
    return False
