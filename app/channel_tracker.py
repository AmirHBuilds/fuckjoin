"""Persistent storage and scheduled cleanup of channels joined by the bot."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.channels import LeaveChannelRequest
from telethon.tl.functions.contacts import BlockRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, DeleteHistoryRequest
from telethon.tl.types import (
    Channel,
    ChatInviteAlready,
    ChatInvitePeek,
    InputPeerChannel,
    PeerChannel,
)

DATA_FILE = Path("data/joined_channels.json")
BOTS_FILE = Path("data/interacted_bots.json")
LEAVE_AFTER_SECONDS = 24 * 3600  # 24 hours


@dataclass
class CleanupResult:
    """Outcome of a full /cleanup run."""

    channels_left: int = 0
    channels_failed: int = 0
    channels_stale: int = 0
    bots_cleaned: int = 0
    bots_failed: int = 0
    rate_limited: bool = False
    channels_remaining: int = 0
    bots_remaining: int = 0


class ChannelTracker:
    """Tracks bot-joined channels and contacted bots without touching personal chats."""

    def __init__(self, data_path: Path = DATA_FILE, bots_path: Path = BOTS_FILE):
        self.path = data_path
        self.bots_path = bots_path
        self.lock = asyncio.Lock()
        self.cleanup_lock = asyncio.Lock()
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        for file in (self.path, self.bots_path):
            file.parent.mkdir(parents=True, exist_ok=True)
            if not file.exists():
                file.write_text("{}", encoding="utf-8")

    def _read_data(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_data(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_bots(self) -> dict[str, dict]:
        try:
            return json.loads(self.bots_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_bots(self, data: dict[str, dict]) -> None:
        self.bots_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def channel_count(self) -> int:
        """Number of channels currently tracked as joined by the bot."""
        return len(self._read_data())

    def bot_count(self) -> int:
        """Number of bot chats currently tracked for cleanup."""
        return len(self._read_bots())

    async def record_bot(self, bot_id: int, username: str | None) -> None:
        """Remember a bot we talked to so /cleanup can delete and block it later."""
        async with self.lock:
            data = self._read_bots()
            data[str(bot_id)] = {"username": username, "seen_at": time.time()}
            self._write_bots(data)

    @staticmethod
    async def _entity_from_invite(client: TelegramClient, invite_hash: str):
        """Return the chat behind an invite hash if we are a member, else None."""
        try:
            info = await client(CheckChatInviteRequest(invite_hash))
        except RPCError:
            return None
        if isinstance(info, (ChatInviteAlready, ChatInvitePeek)):
            return info.chat
        return None

    async def _leave_one(self, client: TelegramClient, ch_id: str) -> bool:
        """Leave one tracked chat. Returns False if we are not a member / it can't be resolved."""
        if ch_id.lstrip("-").isdigit():
            entity = await client.get_input_entity(PeerChannel(abs(int(ch_id))))
        else:
            # Invite hash saved from a join-request link: resolve it to the real chat.
            entity = await self._entity_from_invite(client, ch_id)
            if entity is None:
                return False
        if isinstance(entity, (Channel, InputPeerChannel)):
            await client(LeaveChannelRequest(entity))
        else:
            await client.delete_dialog(entity)  # basic group
        return True

    async def cleanup_all(self, client: TelegramClient, delay: float = 1.5) -> CleanupResult:
        """Leave all tracked channels, then delete + block all tracked bots.

        Entries are dropped from tracking after an attempt (success or normal error).
        On FloodWait the run stops early and untouched entries stay tracked.
        """
        result = CleanupResult()

        async with self.lock:
            channel_ids = list(self._read_data())
            bots = dict(self._read_bots())

        # 1. Leave channels
        for index, ch_id in enumerate(channel_ids):
            try:
                if await self._leave_one(client, ch_id):
                    result.channels_left += 1
                    logging.info("Cleanup: left channel %s", ch_id)
                else:
                    result.channels_stale += 1
            except FloodWaitError as e:
                logging.warning("FloodWait during cleanup: %ss", e.seconds)
                result.rate_limited = True
                result.channels_remaining = len(channel_ids) - index
                result.bots_remaining = len(bots)
                return result
            except Exception as e:
                result.channels_failed += 1
                logging.warning("Cleanup: could not leave channel %s: %s", ch_id, e)

            async with self.lock:
                current = self._read_data()
                current.pop(ch_id, None)
                self._write_data(current)
            await asyncio.sleep(delay)

        # 2. Delete chat history with each bot, then block it
        bot_items = list(bots.items())
        for index, (bot_id, info) in enumerate(bot_items):
            try:
                target = info.get("username") or int(bot_id)
                entity = await client.get_input_entity(target)
                for _ in range(50):  # history is deleted in batches
                    res = await client(DeleteHistoryRequest(peer=entity, max_id=0, revoke=True))
                    if not getattr(res, "offset", 0):
                        break
                await client(BlockRequest(id=entity))
                result.bots_cleaned += 1
                logging.info("Cleanup: deleted and blocked bot %s", target)
            except FloodWaitError as e:
                logging.warning("FloodWait during cleanup: %ss", e.seconds)
                result.rate_limited = True
                result.bots_remaining = len(bot_items) - index
                return result
            except Exception as e:
                result.bots_failed += 1
                logging.warning("Cleanup: could not clean bot %s: %s", bot_id, e)

            async with self.lock:
                current = self._read_bots()
                current.pop(bot_id, None)
                self._write_bots(current)
            await asyncio.sleep(delay)

        return result

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
                        if await self._leave_one(client, ch_id):
                            logging.info("Left tracked channel: %s", ch_id)
                        else:
                            logging.info("Dropped untracked/unjoined entry: %s", ch_id)
                    except FloodWaitError as e:
                        # Keep the entry tracked and retry after the rate limit passes.
                        logging.warning("FloodWait while leaving channels: %ss", e.seconds)
                        await asyncio.sleep(min(e.seconds, 3600) + 1)
                        break
                    except Exception as e:
                        logging.warning("Could not leave channel %s: %s", ch_id, e)
                    async with self.lock:
                        current = self._read_data()
                        current.pop(str(ch_id), None)
                        self._write_data(current)
                    await asyncio.sleep(leave_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error("Error during channel tracker loop: %s", e)