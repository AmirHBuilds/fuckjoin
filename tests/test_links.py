import pytest

from app.links import extract_tme_links, join_target, parse_start_link


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
