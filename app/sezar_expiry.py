"""Detect definitively deleted source messages; never bypass protected forwards."""
from __future__ import annotations

import asyncio
import logging
from typing import Any


class SourceMessageMissing(Exception):
    """A Telegram source message was confirmed absent during delivery."""


# Do not confuse ChatForwardsRestrictedError / group permissions with deletion.
MISSING_ID_ERRORS = frozenset({"MessageIdInvalidError", "MessageIdsEmptyError"})


async def is_verified_expired(client: Any, messages: list[Any], error: BaseException) -> bool:
    """Only return True if Telegram confirms a source message has disappeared.

    A generic forwarding error is NOT evidence that a source was deleted.
    Verification is best-effort; failed lookups conservatively return False.
    """
    if type(error).__name__ not in MISSING_ID_ERRORS:
        return False
    for message in messages:
        if not getattr(message, "media", None) and not getattr(message, "file", None):
            continue
        peer = getattr(message, "peer_id", None)
        msg_id = getattr(message, "id", None)
        if peer is None or msg_id is None:
            continue
        try:
            current = await asyncio.wait_for(client.get_messages(peer, ids=msg_id), timeout=8)
        except Exception as check_error:
            logging.info("Could not confirm source deletion: %s", type(check_error).__name__)
            return False
        if current is None or current == []:
            logging.warning("Source media message no longer exists (chat and message IDs omitted)")
            return True
    return False
