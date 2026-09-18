"""Parsing for bot start and channel join links."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

START_LINK_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{5,})\?([^\s]+)", re.IGNORECASE)
TME_LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/)?[^\s<>()]+", re.IGNORECASE)


@dataclass(frozen=True)
class StartLink:
    bot: str
    argument: str


def parse_start_link(value: str) -> StartLink:
    """Extract a bot username and non-empty `start` argument from a t.me URL."""
    match = START_LINK_RE.search(value)
    if not match:
        raise ValueError("Use a link like https://t.me/bot_name?start=argument")
    query = parse_qs(match.group(2))
    argument = query.get("start", [""])[0]
    if not argument:
        raise ValueError("The link must include a non-empty start argument")
    return StartLink(bot=match.group(1).lower(), argument=argument)


def extract_tme_links(text: str) -> set[str]:
    """Return normalized Telegram URLs found in a text response."""
    return {unquote(match.group(0).rstrip(".,!?")) for match in TME_LINK_RE.finditer(text)}


def join_target(url: str) -> tuple[str, str] | None:
    """Classify a public channel username or a private invite hash from a t.me URL."""
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"t.me", "www.t.me"}:
        return None
    path = parsed.path.strip("/")
    if path.startswith("+"):
        return ("invite", path[1:])
    if path.startswith("joinchat/"):
        return ("invite", path.removeprefix("joinchat/"))
    if re.fullmatch(r"[A-Za-z0-9_]{5,}", path):
        return ("public", path)
    return None
