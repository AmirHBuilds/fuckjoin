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
