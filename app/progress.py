"""Editable Saved Messages progress reporting for queued delivery tasks."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape
from typing import Protocol


class ProgressSink(Protocol):
    """Receives delivery lifecycle events."""

    async def report(self, event: str, *, is_html: bool = False, failed: bool = False) -> None: ...


@dataclass
class ProgressReporter:
    """Append numbered events to one Telegram message instead of spamming messages."""

    message: object
    events: list[tuple[str, bool]] = field(default_factory=list)

    async def report(self, event: str, *, is_html: bool = False, failed: bool = False) -> None:
        self.events.append((event, is_html))
        title = "[FAILED] <b>Delivery failed</b>" if failed else "[WORKING] <b>Delivery in progress</b>"
        rows = "\n".join(
            f"<b>{index}.</b> {item if item_is_html else escape(item)}"
            for index, (item, item_is_html) in enumerate(self.events, start=1)
        )
        await self.message.edit(f"{title}\n<i>Live status</i>\n\n{rows}", parse_mode="html")


@dataclass
class MirroredProgressReporter:
    """Write identical status updates to the requester and the account owner."""

    reporters: tuple[ProgressSink, ...]

    async def report(self, event: str, *, is_html: bool = False, failed: bool = False) -> None:
        for reporter in self.reporters:
            await reporter.report(event, is_html=is_html, failed=failed)
