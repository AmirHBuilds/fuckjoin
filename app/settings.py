"""Configuration parsers and validators for the automation runtime."""

from __future__ import annotations


def parse_retry_delay(value: str | None) -> float:
    """Return a safe delay before retrying a bot after membership changes."""
    try:
        delay = float(value or "2")
    except ValueError as error:
        raise ValueError("RETRY_DELAY_SECONDS must be a number") from error
    if not 0 <= delay <= 60:
        raise ValueError("RETRY_DELAY_SECONDS must be between 0 and 60")
    return delay


def parse_requester_ids(value: str | None) -> set[int]:
    """Parse the explicit Telegram user-ID allow-list for remote requests."""
    if not value:
        return set()
    try:
        return {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as error:
        raise ValueError("ALLOWED_REQUESTER_IDS must contain comma-separated numeric Telegram IDs") from error


def parse_csv_list(value: str | None, default: list[str] | None = None) -> list[str]:
    """Parse comma-separated values into a lowercase list of trimmed strings."""
    if not value:
        return default or []
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def parse_int_setting(value: str | None, default: int) -> int:
    """Parse positive integer setting."""
    try:
        return int(value or default)
    except ValueError:
        return default