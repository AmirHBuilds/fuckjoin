"""Editable progress reporting for queued delivery tasks with FloodWait-safe editing."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from html import escape
from telethon.errors import MessageNotModifiedError
from telethon.errors.rpcerrorlist import FloodWaitError


@dataclass
class ProgressReporter:
    """Manage line events and update a Telegram progress message."""

    message: object
    task_id: str = ""
    events: list[str] = field(default_factory=list)
    last_title: str = "[⏳ Delivery in progress]"
    sent_count: int | None = None
    tip_text: str = "💡 <i>Tip: Send <code>/cancel</code> or <code>cancel</code> to stop.</i>"

    def _render(self, is_active: bool) -> str:
        body = escape("\n".join(self.events))
        footer = (
            f"\n\n<b>[Sent Content Count]</b>: {self.sent_count} message(s)"
            if self.sent_count is not None
            else ""
        )
        tip = f"\n\n{self.tip_text}" if is_active else ""
        return f"<b>{self.last_title}</b>\n<pre>{body}</pre>{footer}{tip}"

    async def report(
        self,
        event: str,
        *,
        failed: bool = False,
        success: bool = False,
        cancelled: bool = False,
        in_place: bool = False,
        sent_count: int | None = None,
    ) -> None:
        is_active = not (failed or success or cancelled)

        if cancelled:
            self.last_title = "[⊘ Delivery cancelled]"
        elif failed:
            self.last_title = "[❌ Delivery failed]"
        elif success:
            self.last_title = "[✅ Delivery completed]"
        else:
            self.last_title = "[⏳ Delivery in progress]"

        if sent_count is not None:
            self.sent_count = sent_count

        if in_place and self.events:
            self.events[-1] = event
        else:
            self.events.append(event)

        text = self._render(is_active=is_active)

        for _ in range(3):
            try:
                await self.message.edit(text, parse_mode="html")
                break
            except MessageNotModifiedError:
                break
            except FloodWaitError as err:
                logging.warning("FloodWait while updating progress card: %ss", err.seconds)
                if err.seconds > 10:
                    break
                await asyncio.sleep(err.seconds + 1)
            except Exception as err:
                logging.debug("Failed updating progress message: %s", err)
                break

    def get_rendered_text(self) -> str:
        """Return the plain-text lines of all progress steps sent to the user."""
        return "\n".join(self.events)