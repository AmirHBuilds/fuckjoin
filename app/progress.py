"""Editable Saved Messages progress reporting for queued delivery tasks."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProgressReporter:
    """Append numbered events to one Telegram message instead of spamming messages."""

    message: object
    events: list[str] = field(default_factory=list)

    async def report(self, event: str) -> None:
        self.events.append(event)
        body = "\n".join(f"{index}. {item}" for index, item in enumerate(self.events, start=1))
        await self.message.edit(body)
