"""The whole path with the real client: precheck -> defer -> one HTTP call -> edit (Req 1.3, 7.1).

Everything else stubs the client; this test does not, so the request the bot actually puts on the
wire and the reply it actually renders are checked together.
"""

import json

import httpx

from mcclub_rag.discordbot.app import handle_ask, is_member
from mcclub_rag.discordbot.client import RagClient
from mcclub_rag.discordbot.commands import AskRequest
from mcclub_rag.discordbot.notices import RATE_LIMITED, SERVICE_UNAVAILABLE, notice
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from tests.bot_fakes import FakeInteraction, FakeRole, FakeUser, chat_response

MEMBER_TOKEN = "member-secret"


def _settings() -> BotSettings:
    return BotSettings(
        _env_file=None,
        bot_token="test-token",
        guilds="42:7",
        member_token=MEMBER_TOKEN,
        user_cooldown_s=20.0,
        max_concurrency=4,
        api_timeout_s=5.0,
    )


def _client(handler) -> RagClient:  # noqa: ANN001 - a one-line factory for the tests below
    return RagClient(
        "http://app:8000",
        timeout_s=5.0,
        member_token=MEMBER_TOKEN,
        transport=httpx.MockTransport(handler),
    )


async def test_a_members_question_travels_as_an_authenticated_request_and_renders() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat_response().model_dump())

    settings = _settings()
    client = _client(handler)
    interaction = FakeInteraction(
        user=FakeUser(roles=(FakeRole(7),), display_name="Nadia"),
    )
    request = AskRequest(
        question="Where is the club?",
        public=False,
        user_id=7,
        asker="Nadia",
        guild_id=42,
        channel_id=99,
        as_member=is_member(interaction, settings),
    )

    await handle_ask(
        interaction,
        request,
        client=client,
        cooldown=UserCooldown(settings.user_cooldown_s),
        slots=GuildSlots(settings.max_concurrency),
        conversations=ConversationDirectory(),
    )
    await client.aclose()

    assert interaction.events == ["defer", "edit_original_response"]
    assert seen["path"] == "/api/v1/chat"
    assert seen["auth"] == f"Bearer {MEMBER_TOKEN}"
    assert seen["body"] == {"message": "Where is the club?"}
    edit = interaction.response.edits[0]
    assert edit["embed"].description == chat_response().answer
    assert edit["embed"].fields[0].name == "Club rules"
    assert "view" in edit


async def test_a_second_question_in_the_same_channel_carries_the_conversation_id() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response().model_dump())

    client = _client(handler)
    conversations = ConversationDirectory()
    runtime = {
        "client": client,
        "cooldown": UserCooldown(0.0),
        "slots": GuildSlots(4),
        "conversations": conversations,
    }
    request = AskRequest(
        question="Where is the club?",
        public=False,
        user_id=7,
        asker="Sam",
        guild_id=42,
        channel_id=99,
        as_member=False,
    )

    await handle_ask(FakeInteraction(), request, **runtime)
    await handle_ask(FakeInteraction(), request, **runtime)
    await client.aclose()

    assert bodies[0] == {"message": "Where is the club?"}
    assert bodies[1]["conversation_id"] == chat_response().conversation_id


async def test_a_rate_limited_api_becomes_the_localized_notice_and_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": "rate_limited",
                    "message": "Too many chat requests. Please try again shortly.",
                }
            },
        )

    client = _client(handler)
    interaction = FakeInteraction()
    request = AskRequest(
        question="Combien coûte l'adhésion au club cette année ?",
        public=False,
        user_id=7,
        asker="Sam",
        guild_id=42,
        channel_id=99,
        as_member=False,
    )

    await handle_ask(
        interaction,
        request,
        client=client,
        cooldown=UserCooldown(0.0),
        slots=GuildSlots(4),
        conversations=ConversationDirectory(),
    )
    await client.aclose()

    assert calls == 1  # Req 6.3, 6.2
    assert interaction.response.edits[0]["content"] == notice(RATE_LIMITED, "fr")
    assert interaction.response.edits[0]["embed"] is None


async def test_an_unreachable_api_becomes_a_notice_not_a_traceback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    interaction = FakeInteraction()
    request = AskRequest(
        question="Where is the club?",
        public=False,
        user_id=7,
        asker="Sam",
        guild_id=42,
        channel_id=99,
        as_member=True,
    )

    await handle_ask(
        interaction,
        request,
        client=client,
        cooldown=UserCooldown(0.0),
        slots=GuildSlots(4),
        conversations=ConversationDirectory(),
    )
    await client.aclose()

    assert interaction.response.edits[0]["content"] == notice(SERVICE_UNAVAILABLE, "en")
    assert "error" not in interaction.response.edits[0]["content"].lower()
    assert interaction.followup.sent == []
