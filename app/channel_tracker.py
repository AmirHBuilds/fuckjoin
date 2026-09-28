"""Persistent storage and scheduled cleanup of channels joined by the bot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from telethon import TelegramClient
from telethon.tl.functions.channels import LeaveChannelRequest

DATA_FILE = Path("data/joined_channels.json")
LEAVE_AFTER_SECONDS = 24 * 3600  # 24 hours


class ChannelTracker:
    """Tracks bot-joined channels to leave them without touching personal channels."""

    def __init__(self, data_path: Path = DATA_FILE):
        self.path = data_path
        self.lock = asyncio.Lock()
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _read_data(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_data(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def record_join(self, channel_id: int | str) -> None:
        """Record a joined channel with current timestamp."""
        async with self.lock:
            data = self._read_data()
            data[str(channel_id)] = {"joined_at": time.time()}
            self._write_data(data)

    async def run_cleanup_loop(
        self, client: TelegramClient, max_channels: int, leave_delay: float = 3.0
    ) -> None:
        """Background worker that leaves channels older than 24h or when limit is exceeded."""
        while True:
            try:
                await asyncio.sleep(60)  # Check every minute
                async with self.lock:
                    data = self._read_data()
                    now = time.time()
                    to_leave: list[str] = []

                    # 1. Collect channels joined > 24 hours ago
                    for ch_id, info in list(data.items()):
                        if now - info.get("joined_at", 0) >= LEAVE_AFTER_SECONDS:
                            to_leave.append(ch_id)

                    # 2. Check channel overflow (> max_channels)
                    active_channels = sorted(
                        [k for k in data if k not in to_leave],
                        key=lambda k: data[k].get("joined_at", 0),
                    )
                    if len(active_channels) > max_channels:
                        # Schedule oldest half to be left
                        excess = active_channels[: len(active_channels) // 2]
                        to_leave.extend(excess)

                # Process channel exits with delay
                for ch_id in to_leave:
                    try:
                        entity_id = int(ch_id) if ch_id.lstrip("-").isdigit() else ch_id
                        entity = await client.get_input_entity(entity_id)
                        await client(LeaveChannelRequest(entity))
                        logging.info("Left tracked channel: %s", ch_id)
                    except Exception as e:
                        logging.warning("Could not leave channel %s: %s", ch_id, e)
                    finally:
                        async with self.lock:
                            current = self._read_data()
                            current.pop(str(ch_id), None)
                            self._write_data(current)
                        await asyncio.sleep(leave_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error during channel tracker loop: %s", e)