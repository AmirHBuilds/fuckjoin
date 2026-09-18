"""Small configuration validators for the automation runtime."""


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
