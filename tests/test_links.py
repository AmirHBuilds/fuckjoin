import pytest

from app.links import extract_tme_links, join_target, parse_start_link
from app.responses import is_transient_response


class FakeMessage:
    def __init__(self, text: str, media: object | None = None, buttons: object | None = None) -> None:
        self.raw_text = text
        self.media = media
        self.buttons = buttons


def test_parses_bot_start_link() -> None:
    assert parse_start_link("/get https://t.me/UltraBot?start=abc_123") == (
        parse_start_link("https://t.me/ultrabot?start=abc_123")
    )


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
