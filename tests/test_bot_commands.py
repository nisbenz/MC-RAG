"""The three commands' decisions (Req 1.4, 1.5, 3.3, 4, 6, 7)."""

import pytest

from mcclub_rag.discordbot.client import ApiFailure, ChatOutcome, HealthOutcome
from mcclub_rag.discordbot.commands import (
    MAX_QUESTION_CHARS,
    AskRequest,
    precheck,
    run_ask,
    run_reset,
    run_status,
)
from mcclub_rag.discordbot.notices import (
    BUSY,
    RATE_LIMITED,
    RESET_DONE,
    SERVICE_UNAVAILABLE,
    STATUS,
    TOO_LONG,
    UNAUTHORIZED,
    notice,
    notice_language,
)
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from tests.bot_fakes import FakeRagClient, chat_response

ARABIC_QUESTION = "كم رسوم العضوية في النادي هذا العام؟"


def _request(question: str = "Where is the club?", **overrides: object) -> AskRequest:
    values: dict[str, object] = {
        "question": question,
        "public": False,
        "user_id": 7,
        "asker": "Sam",
        "guild_id": 42,
        "channel_id": 99,
        "as_member": False,
    }
    values.update(overrides)
    return AskRequest(**values)  # type: ignore[arg-type]


def _guards(*, cooldown_s: float = 0.0, max_concurrency: int = 4) -> tuple:
    return UserCooldown(cooldown_s), GuildSlots(max_concurrency)


def _settings(**overrides: object) -> BotSettings:
    values: dict[str, object] = {"bot_token": "test-token"}
    values.update(overrides)
    return BotSettings(_env_file=None, **values)  # type: ignore[arg-type]


def _text(reply) -> str:  # noqa: ANN001 - a tiny test helper
    return "\n".join(part.content for part in reply.parts)


def test_precheck_passes_and_holds_a_slot() -> None:
    cooldown, slots = _guards()
    assert precheck(_request(), cooldown=cooldown, slots=slots) is None
    assert slots.in_flight(42) == 1


def test_precheck_refuses_an_over_long_question() -> None:
    cooldown, slots = _guards()
    request = _request("x" * (MAX_QUESTION_CHARS + 1))
    reply = precheck(request, cooldown=cooldown, slots=slots)
    assert reply is not None
    assert _text(reply) == notice(TOO_LONG, "en")
    assert slots.in_flight(42) == 0


def test_precheck_accepts_exactly_the_limit() -> None:
    cooldown, slots = _guards()
    assert precheck(_request("x" * MAX_QUESTION_CHARS), cooldown=cooldown, slots=slots) is None


def test_precheck_refuses_inside_the_cooldown_and_frees_the_slot() -> None:
    cooldown, slots = _guards(cooldown_s=20)
    assert precheck(_request(), cooldown=cooldown, slots=slots) is None
    reply = precheck(_request(), cooldown=cooldown, slots=slots)
    assert reply is not None
    assert "20 s" in _text(reply)
    assert slots.in_flight(42) == 1  # only the first, allowed ask still holds one


def test_precheck_refuses_when_the_guild_is_at_its_cap() -> None:
    cooldown, slots = _guards(max_concurrency=1)
    assert precheck(_request(), cooldown=cooldown, slots=slots) is None
    reply = precheck(_request(user_id=8), cooldown=cooldown, slots=slots)
    assert reply is not None
    assert _text(reply) == notice(BUSY, "en")


def test_precheck_lets_a_blank_question_through() -> None:
    cooldown, slots = _guards()
    assert precheck(_request("   "), cooldown=cooldown, slots=slots) is None


async def test_first_turn_omits_the_conversation_id() -> None:
    client = FakeRagClient()
    conversations = ConversationDirectory()
    outcome = await run_ask(_request(), client=client, conversations=conversations)
    assert client.asks[0]["conversation_id"] is None
    assert outcome.failure is None
    assert conversations.get(42, 99) == "c_test"


async def test_later_turns_send_the_id_the_api_returned() -> None:
    client = FakeRagClient()
    conversations = ConversationDirectory()
    await run_ask(_request(), client=client, conversations=conversations)
    await run_ask(_request(), client=client, conversations=conversations)
    assert client.asks[1]["conversation_id"] == "c_test"


async def test_a_ttl_reset_is_adopted_not_reported() -> None:
    """Req 4.2: the API forgetting the conversation looks like a normal first turn."""
    conversations = ConversationDirectory()
    conversations.remember(42, 99, "c_expired")
    client = FakeRagClient(ChatOutcome(response=chat_response(conversation_id="c_fresh")))
    outcome = await run_ask(_request(), client=client, conversations=conversations)
    assert client.asks[0]["conversation_id"] == "c_expired"
    assert outcome.failure is None
    assert conversations.get(42, 99) == "c_fresh"


async def test_membership_travels_to_the_client() -> None:
    client = FakeRagClient()
    await run_ask(_request(as_member=True), client=client, conversations=ConversationDirectory())
    assert client.asks[0]["as_member"] is True


async def test_the_question_is_forwarded_verbatim() -> None:
    client = FakeRagClient()
    question = "  Mixte: où est le club?  "
    await run_ask(_request(question), client=client, conversations=ConversationDirectory())
    assert client.asks[0]["question"] == question


@pytest.mark.parametrize(
    ("code", "table"),
    [
        ("unauthorized", UNAUTHORIZED),
        ("rate_limited", RATE_LIMITED),
        ("index_unavailable", SERVICE_UNAVAILABLE),
        ("validation_error", SERVICE_UNAVAILABLE),
        ("unreachable", SERVICE_UNAVAILABLE),
        ("timeout", SERVICE_UNAVAILABLE),
        ("internal", SERVICE_UNAVAILABLE),
    ],
)
async def test_every_failure_code_becomes_its_notice(code: str, table: dict) -> None:
    client = FakeRagClient(ChatOutcome(failure=ApiFailure(code, 500)))
    outcome = await run_ask(_request(), client=client, conversations=ConversationDirectory())
    assert outcome.failure is not None
    assert outcome.failure.code == code
    assert _text(outcome.reply) == notice(table, "en")
    assert len(client.asks) == 1  # never retried (Req 6.3)


async def test_a_failure_does_not_remember_a_conversation() -> None:
    conversations = ConversationDirectory()
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("rate_limited", 429)))
    await run_ask(_request(), client=client, conversations=conversations)
    assert conversations.get(42, 99) is None


async def test_a_failure_keeps_the_existing_conversation() -> None:
    conversations = ConversationDirectory()
    conversations.remember(42, 99, "c_abc")
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("rate_limited", 429)))
    await run_ask(_request(), client=client, conversations=conversations)
    assert conversations.get(42, 99) == "c_abc"


async def test_an_arabic_question_gets_an_arabic_notice() -> None:
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("rate_limited", 429)))
    outcome = await run_ask(
        _request(ARABIC_QUESTION, user_id=9),
        client=client,
        conversations=ConversationDirectory(),
    )
    assert notice_language(ARABIC_QUESTION) == "ar"
    assert _text(outcome.reply) == notice(RATE_LIMITED, "ar")


async def test_an_arabic_answer_is_rendered_in_arabic() -> None:
    client = FakeRagClient(ChatOutcome(response=chat_response(answer="النادي في الورشة.")))
    outcome = await run_ask(
        _request(ARABIC_QUESTION), client=client, conversations=ConversationDirectory()
    )
    assert outcome.reply.parts[0].embed.description == "النادي في الورشة."


def test_reset_forgets_the_channel() -> None:
    conversations = ConversationDirectory()
    conversations.remember(42, 99, "c_abc")
    reply = run_reset(42, 99, conversations)
    assert conversations.get(42, 99) is None
    assert _text(reply) == notice(RESET_DONE, "en")


def test_reset_does_not_touch_another_channel() -> None:
    conversations = ConversationDirectory()
    conversations.remember(42, 100, "c_other")
    run_reset(42, 99, conversations)
    assert conversations.get(42, 100) == "c_other"


async def test_status_reports_health_and_the_bots_own_guards() -> None:
    client = FakeRagClient(health=HealthOutcome(status="degraded", documents=12))
    reply = await run_status(client=client, settings=_settings(max_concurrency=3))
    assert client.health_calls == 1
    assert _text(reply) == notice(
        STATUS, "en", status="degraded", documents=12, cooldown="20", concurrency=3
    )


async def test_status_reports_an_unreachable_api() -> None:
    client = FakeRagClient(health=HealthOutcome(failure=ApiFailure("unreachable")))
    reply = await run_status(client=client, settings=_settings())
    assert _text(reply) == notice(SERVICE_UNAVAILABLE, "en")
