"""Helpers for deciding whether a bot message is ready to process."""

from __future__ import annotations


def is_transient_response(message: object, transient_texts: list[str]) -> bool:
    """Identify a bare wait indicator, rather than a bot's actual response."""
    text = getattr(message, "raw_text", "").strip().lower()
    has_buttons = bool(getattr(message, "buttons", None))
    is_transient = any(t in text for t in transient_texts) if transient_texts else False
    return is_transient and not getattr(message, "media", None) and not has_buttons


def is_message_blocked(message: object, blocked_words: list[str]) -> bool:
    """Check if message is a text-only message containing blocked words.
    
    Media with captions are exempt from this filter.
    """
    if getattr(message, "media", None) is not None:
        return False
    raw_text = getattr(message, "raw_text", "").lower()
    return any(word in raw_text for word in blocked_words if word)