"""The in-memory guards and the localized notices (Req 4, 6.1, 7.2)."""

import pytest

import mcclub_rag.discordbot.notices as notices_module
from mcclub_rag.discordbot.notices import (
    STATUS,
    NoticeLanguage,
    notice,
    notice_language,
)
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown

_TABLES = {
    name: value
    for name, value in vars(notices_module).items()
    if isinstance(value, dict) and name.isupper()
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_directory_remembers_and_forgets() -> None:
    directory = ConversationDirectory()
    assert directory.get(42, 99) is None
    directory.remember(42, 99, "c_abc")
    assert directory.get(42, 99) == "c_abc"
    directory.forget(42, 99)
    assert directory.get(42, 99) is None


def test_directory_separates_guilds_and_dms() -> None:
    directory = ConversationDirectory()
    directory.remember(1, 99, "c_one")
    directory.remember(2, 99, "c_two")
    directory.remember(None, 99, "c_dm")
    assert directory.get(1, 99) == "c_one"
    assert directory.get(2, 99) == "c_two"
    assert directory.get(None, 99) == "c_dm"
    directory.forget(1, 99)
    assert directory.get(2, 99) == "c_two"


def test_directory_key_is_namespaced() -> None:
    assert ConversationDirectory.key(42, 99) == "mc-42-99"
    assert ConversationDirectory.key(None, 99) == "mc-dm-99"


def test_cooldown_refuses_then_clears() -> None:
    clock = Clock()
    cooldown = UserCooldown(20, clock=clock)
    assert cooldown.acquire(7) == 0.0
    assert cooldown.acquire(7) == pytest.approx(20.0)
    clock.advance(5)
    assert cooldown.acquire(7) == pytest.approx(15.0)
    clock.advance(16)
    assert cooldown.acquire(7) == 0.0


def test_cooldown_is_per_user() -> None:
    cooldown = UserCooldown(20, clock=Clock())
    assert cooldown.acquire(7) == 0.0
    assert cooldown.acquire(8) == 0.0


def test_zero_cooldown_never_refuses() -> None:
    cooldown = UserCooldown(0, clock=Clock())
    assert cooldown.acquire(7) == 0.0
    assert cooldown.acquire(7) == 0.0


def test_guild_slots_cap_and_release() -> None:
    slots = GuildSlots(2)
    assert slots.acquire(42) is True
    assert slots.acquire(42) is True
    assert slots.acquire(42) is False
    assert slots.in_flight(42) == 2
    assert slots.acquire(43) is True  # another guild is unaffected
    slots.release(42)
    assert slots.in_flight(42) == 1
    assert slots.acquire(42) is True
    slots.release(42)
    slots.release(42)
    assert slots.in_flight(42) == 0
    slots.release(42)  # never goes negative
    assert slots.in_flight(42) == 0


def test_notice_language_follows_the_script() -> None:
    assert notice_language("من فضلك أخبرني أين يقع النادي ومتى يجتمع الأعضاء") == "ar"
    assert notice_language("Pouvez-vous me dire où se trouve le club exactement ?") == "fr"
    assert notice_language("Could you tell me where the club is located exactly?") == "en"


def test_notice_falls_back_to_english() -> None:
    unknown: NoticeLanguage = "en"
    assert notice(STATUS, unknown) == notice(STATUS, "en")
    assert notice(STATUS, unknown, status="ok", documents=1, cooldown="20", concurrency=4)


@pytest.mark.parametrize("name", sorted(_TABLES))
def test_every_table_carries_all_three_languages(name: str) -> None:
    assert set(_TABLES[name]) == {"en", "fr", "ar"}, name


def test_status_notice_formats_its_values() -> None:
    text = notice(STATUS, "en", status="ok", documents=60, cooldown="20", concurrency=4)
    assert "ok" in text and "60" in text and "20" in text and "4" in text
