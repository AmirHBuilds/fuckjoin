"""Editable progress reporting for queued delivery tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Protocol


class ProgressSink(Protocol):
    """Receives delivery lifecycle events."""

    async def report(
        self,
        event: str,
        *,
        failed: bool = False,
        success: bool = False,
        in_place: bool = False,
        sent_count: int | None = None,
    ) -> None: ...

    def get_rendered_text(self) -> str: ...


@dataclass
class ProgressReporter:
    """Manage line events and update a Telegram progress message."""

    message: object
    events: list[str] = field(default_factory=list)
    last_title: str = "[⏳ Delivery in progress]"
    sent_count: int | None = None

    def _render(self) -> str:
        body = escape("\n".join(self.events))
        footer = f"\n\n[Sent Content Count]: {self.sent_count} message(s)" if self.sent_count is not None else ""
        return f"<b>{self.last_title}</b>\n<pre>{body}</pre>{footer}"

    async def report(
        self,
        event: str,
        *,
        failed: bool = False,
        success: bool = False,
        in_place: bool = False,
        sent_count: int | None = None,
    ) -> None:
        if failed:
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

        await self.message.edit(self._render(), parse_mode="html")

    def get_rendered_text(self) -> str:
        """Return the plain-text lines of all progress steps sent to the user."""
        return "\n".join(self.events)