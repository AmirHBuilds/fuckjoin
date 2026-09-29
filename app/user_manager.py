"""Persistent storage for dynamically allowed users."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

USERS_FILE = Path("data/allowed_users.json")


class UserManager:
    """Manages dynamically allowed user IDs persisted on disk."""

    def __init__(self, data_path: Path = USERS_FILE):
        self.path = data_path
        self.lock = asyncio.Lock()
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _read_users(self) -> set[int]:
        try:
            return set(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            return set()

    def _write_users(self, users: set[int]) -> None:
        self.path.write_text(json.dumps(sorted(list(users)), indent=2), encoding="utf-8")

    async def get_allowed_users(self) -> set[int]:
        async with self.lock:
            return self._read_users()

    async def add_user(self, user_id: int) -> bool:
        """Add user ID. Returns True if added, False if already present."""
        async with self.lock:
            users = self._read_users()
            if user_id in users:
                return False
            users.add(user_id)
            self._write_users(users)
            return True

    async def remove_user(self, user_id: int) -> bool:
        """Remove user ID. Returns True if removed, False if wasn't present."""
        async with self.lock:
            users = self._read_users()
            if user_id not in users:
                return False
            users.remove(user_id)
            self._write_users(users)
            return True