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
    ) -> None: ...

    def get_rendered_text(self) -> str: ...


@dataclass
class ProgressReporter:
    """Manage line events and update a Telegram progress message."""

    message: object
    events: list[str] = field(default_factory=list)
    last_title: str = "[⏳ Delivery in progress]"

    def _render(self, title: str) -> str:
        body = escape("\n".join(self.events))
        return f"<b>{title}</b>\n\n<pre>{body}</pre>"

    async def report(
        self,
        event: str,
        *,
        failed: bool = False,
        success: bool = False,
        in_place: bool = False,
    ) -> None:
        if failed:
            self.last_title = "[❌ Delivery failed]"
        elif success:
            self.last_title = "[✅ Delivery completed]"
        else:
            self.last_title = "[⏳ Delivery in progress]"

        if in_place and self.events:
            self.events[-1] = event
        else:
            self.events.append(event)

        await self.message.edit(self._render(self.last_title), parse_mode="html")

    def get_rendered_text(self) -> str:
        """Return the plain-text lines of all progress steps sent to the user."""
        return "\n".join(self.events)