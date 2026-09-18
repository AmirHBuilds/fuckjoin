"""Helpers for deciding whether a bot message is ready to process."""

TRANSIENT_TEXTS = {"⏳", "⌛"}


def is_transient_response(message: object) -> bool:
    """Identify a bare wait indicator, rather than a bot's actual response."""
    text = getattr(message, "raw_text", "").strip()
    has_buttons = bool(getattr(message, "buttons", None))
    return text in TRANSIENT_TEXTS and not getattr(message, "media", None) and not has_buttons
