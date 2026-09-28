"""Parsing for bot start and channel join links."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse
from telethon.tl.types import MessageEntityTextUrl

START_LINK_RE = re.compile(r"https?://t\.me/([A-Za-z0-9_]{5,})\?([^\s]+)", re.IGNORECASE)
TME_LINK_RE = re.compile(r"https?://t\.me/(?:joinchat/)?[^\s<>()]+", re.IGNORECASE)


@dataclass(frozen=True)
class StartLink:
    bot: str
    argument: str
    url: str = ""
    label: str = ""


def parse_start_link(value: str) -> StartLink:
    """Extract a bot username and non-empty `start` argument from a t.me URL."""
    match = START_LINK_RE.search(value)
    if not match:
        raise ValueError("Use a link like https://t.me/bot_name?start=argument")
    query = parse_qs(match.group(2))
    argument = query.get("start", [""])[0]
    if not argument:
        raise ValueError("The link must include a non-empty start argument")
    return StartLink(bot=match.group(1).lower(), argument=argument, url=match.group(0))


def extract_bot_start_links_with_labels(message: object) -> list[StartLink]:
    """Find all valid bot start links from message text and formatted entities."""
    raw_text = getattr(message, "raw_text", "") or ""
    entities = getattr(message, "entities", None) or []
    results: list[StartLink] = []
    seen_urls: set[str] = set()

    # 1. Scan formatted text URLs (e.g. hyperlinks inside words)
    for entity in entities:
        if isinstance(entity, MessageEntityTextUrl) and entity.url:
            try:
                parsed = parse_start_link(entity.url)
                if parsed.url not in seen_urls:
                    seen_urls.add(parsed.url)
                    # Extract anchor label from the text slice
                    label = raw_text[entity.offset : entity.offset + entity.length].strip()
                    results.append(
                        StartLink(
                            bot=parsed.bot,
                            argument=parsed.argument,
                            url=parsed.url,
                            label=label or f"@{parsed.bot}",
                        )
                    )
            except ValueError:
                pass

    # 2. Scan plain text URLs
    for match in START_LINK_RE.finditer(raw_text):
        url = match.group(0)
        if url not in seen_urls:
            seen_urls.add(url)
            try:
                parsed = parse_start_link(url)
                results.append(
                    StartLink(
                        bot=parsed.bot,
                        argument=parsed.argument,
                        url=parsed.url,
                        label=f"@{parsed.bot}",
                    )
                )
            except ValueError:
                pass

    return results


def find_start_links(urls: set[str]) -> list[StartLink]:
    """Return valid bot deep links from an arbitrary collection of Telegram URLs."""
    start_links: list[StartLink] = []
    for url in urls:
        try:
            start_links.append(parse_start_link(url))
        except ValueError:
            continue
    return start_links


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