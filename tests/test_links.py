import asyncio

import pytest

from app.links import extract_tme_links, find_start_links, join_target, parse_start_link
from app.responses import is_transient_response
from app.settings import parse_retry_delay
from app.progress import ProgressReporter


class FakeMessage:
    def __init__(self, text: str, media: object | None = None, buttons: object | None = None) -> None:
        self.raw_text = text
        self.media = media
        self.buttons = buttons


def test_parses_bot_start_link() -> None:
    assert parse_start_link("/get https://t.me/UltraBot?start=abc_123") == (
        parse_start_link("https://t.me/ultrabot?start=abc_123")
    )


def test_finds_only_valid_bot_start_links() -> None:
    links = {
        "https://t.me/second_bot?start=next",
        "https://t.me/channel_one",
    }

    assert find_start_links(links) == [parse_start_link("https://t.me/second_bot?start=next")]


@pytest.mark.parametrize("value", ["https://t.me/bot", "https://example.com/bot?start=x"])
def test_rejects_invalid_start_links(value: str) -> None:
    with pytest.raises(ValueError):
        parse_start_link(value)


def test_finds_and_classifies_public_and_invite_links() -> None:
    links = extract_tme_links("Join https://t.me/channel_one and https://t.me/+InviteHash.")

    assert join_target("https://t.me/channel_one") == ("public", "channel_one")
    assert join_target("https://t.me/+InviteHash") == ("invite", "InviteHash")
    assert len(links) == 2


def test_only_bare_wait_indicator_is_transient() -> None:
    assert is_transient_response(FakeMessage("⏳"))
    assert not is_transient_response(FakeMessage("⏳", media=object()))
    assert not is_transient_response(FakeMessage("⏳", buttons=[[object()]]))


def test_parses_a_bounded_retry_delay() -> None:
    assert parse_retry_delay(None) == 2
    assert parse_retry_delay("1.5") == 1.5
    with pytest.raises(ValueError):
        parse_retry_delay("61")


def test_progress_reporter_edits_one_message_with_numbered_events() -> None:
    class FakeStatusMessage:
        def __init__(self) -> None:
            self.body = ""

        async def edit(self, body: str) -> None:
            self.body = body

    message = FakeStatusMessage()
    reporter = ProgressReporter(message)
    asyncio.run(reporter.report("Started"))
    asyncio.run(reporter.report("Joining channel"))

    assert message.body == "1. Started\n2. Joining channel"
