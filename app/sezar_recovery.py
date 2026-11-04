"""Conservative restart-on-stale-response support for Sezar/fuckjoin.

A recovery is attempted from the ORIGINAL link only when no media was delivered.
The code waits for Telegram's FloodWait rather than bypassing it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextvars import ContextVar
from typing import Any, Awaitable

# A per-worker-task flag. None means that we're outside a delivery attempt.
_waited: ContextVar[bool | None] = ContextVar('sezar_delivery_waited', default=None)


def mark_explicit_wait(duration: float) -> None:
    if duration > 10 and _waited.get() is not None:
        _waited.set(True)


async def measure_telegram_call(aw: Awaitable[Any]) -> Any:
    """Record unexpectedly slow Telegram calls (including automatic Telethon sleeps)."""
    start = time.monotonic()
    try:
        return await aw
    finally:
        if time.monotonic() - start > 10 and _waited.get() is not None:
            _waited.set(True)


def _limit(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        return max(min_value, min(max_value, int(os.environ.get(name, default))))
    except (ValueError, TypeError):
        return default


async def deliver_with_retries(deliver, *, progress, cancel_event, cancel_error, **kwargs):
    """Original-link retry, plus one optional extra attempt for proven deletion.

    Never replay on partially completed delivery or protected forwarding.
    """
    normal = _limit('RECOVERY_MAX_ATTEMPTS', 3, 1, 3)
    extra = _limit('RECOVERY_EXTRA_EXPIRED_ATTEMPTS', 1, 0, 1)
    maximum = normal + extra
    threshold = _limit('RECOVERY_RESTART_AFTER_SECONDS', 10, 10, 3600)
    status, messages = '[✗] No media delivered', []
    for attempt in range(1, maximum + 1):
        if cancel_event.is_set():
            raise cancel_error()
        token = _waited.set(False)
        started = time.monotonic()
        retryable = False
        missing = False
        try:
            try:
                status, messages = await deliver(progress=progress, cancel_event=cancel_event, **kwargs)
            except (TimeoutError, TypeError) as exc:
                retryable = True
                logging.warning('Bot response timed out or was malformed on attempt %d: %s', attempt, type(exc).__name__)  # SEZAR-TIMEOUT-DIAGNOSTICS v4.2
                status, messages = f'[✗] Flow error: {type(exc).__name__}', []
            except Exception as exc:
                if type(exc).__name__ == 'SourceMessageMissing':
                    missing = True
                    retryable = True
                    status, messages = '[✗] Source media was deleted before forwarding', []
                    logging.warning('Source media disappeared on delivery attempt %s', attempt)
                elif type(exc).__name__ == 'FloodWaitError' and hasattr(exc, 'seconds'):
                    seconds = max(0, int(exc.seconds))
                    limit = _limit('RECOVERY_MAX_FLOODWAIT_SECONDS', 900, 30, 3600)
                    if seconds > limit:
                        return f'[✗] Telegram requires {seconds}s of cooldown; no immediate retry.', []
                    await progress.report(f'[⏳] Telegram cooldown {seconds}s; waiting before retry')
                    mark_explicit_wait(seconds)
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=seconds + 1)
                        raise cancel_error()
                    except asyncio.TimeoutError:
                        pass
                    retryable = True
                    status, messages = '[✗] Telegram cooldown ended before media delivery', []
                else:
                    raise
        finally:
            waited = _waited.get()
            _waited.reset(token)
        if messages:
            return status, messages  # Never repeat any successfully delivered content.
        slow = bool(waited) or (time.monotonic() - started >= threshold)
        if attempt < normal:
            should_retry = slow or retryable
        else:
            # Extra attempt is ONLY for a confirmed missing source message on
            # the final normal attempt. Not for protected content or permissions.
            should_retry = missing and attempt < maximum
        if not should_retry:
            return status, []
        await progress.report(
            f'[↻] Source expired / step delayed. Restarting original link ({attempt + 1}/{maximum})...'
        )
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=3)
            raise cancel_error()
        except asyncio.TimeoutError:
            pass
    return status, []

