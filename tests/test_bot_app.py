"""The Discord wiring: defer order, delivery, roles, expiry, logging (Req 1, 3, 7)."""

import json

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from mcclub_rag.discordbot.app import (
    build_client,
    handle_ask,
    handle_reset,
    handle_status,
    is_member,
    sync_commands,
    to_embed,
)
from mcclub_rag.discordbot.client import ApiFailure, ChatOutcome, RagClient
from mcclub_rag.discordbot.commands import AskRequest
from mcclub_rag.discordbot.notices import ANSWER_UNDELIVERED, UNAUTHORIZED, notice
from mcclub_rag.discordbot.render import EmbedSpec, FieldSpec
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from tests.bot_fakes import (
    ANSWER_TEXT,
    FakeInteraction,
    FakeRagClient,
    FakeRole,
    FakeUser,
    chat_response,
    expired_interaction_error,
)

LONG_ANSWER = "\n\n".join(f"paragraph {index} " + "x" * 300 for index in range(20))
SECRET = "member-secret"
MARKER = "Zq7"


def _request(**overrides: object) -> AskRequest:
    values: dict[str, object] = {
        "question": f"Where is the clubhouse and what is the passphrase {MARKER}?",
        "public": False,
        "user_id": 7,
        "asker": "Sam",
        "guild_id": 42,
        "channel_id": 99,
        "as_member": False,
    }
    values.update(overrides)
    return AskRequest(**values)  # type: ignore[arg-type]


def _settings(**overrides: object) -> BotSettings:
    values: dict[str, object] = {"bot_token": "test-token", "guild_id": 42, "member_role_id": 7}
    values.update(overrides)
    return BotSettings(_env_file=None, **values)  # type: ignore[arg-type]


def _runtime(
    client: FakeRagClient,
    *,
    cooldown_s: float = 0.0,
    max_concurrency: int = 4,
) -> dict:
    return {
        "client": client,
        "cooldown": UserCooldown(cooldown_s),
        "slots": GuildSlots(max_concurrency),
        "conversations": ConversationDirectory(),
    }


class ExpiringInteraction(FakeInteraction):
    """What an interaction looks like once its 15-minute token has died."""

    async def edit_original_response(self, **kwargs: object) -> None:
        raise expired_interaction_error()


def _wire(client: object, monkeypatch: pytest.MonkeyPatch, method: str, fake) -> None:  # noqa: ANN001
    """Capture the payload discord.py would put on the wire, without a connection."""
    # The tree refuses to sync without an application id, which normally arrives with READY.
    client._connection.application_id = 1560638956083609730  # noqa: SLF001 - test seam
    monkeypatch.setattr(client.http, method, fake)


async def test_a_configured_guild_receives_the_commands_in_the_synced_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1.1: with `DISCORD_GUILD_ID` set, the three commands must reach that guild.

    `@tree.command` registers in `_global_commands`, while discord.py's `sync(guild=...)` sends only
    the commands registered *for that guild* (see `_get_all_commands`), so without
    `copy_global_to` the PUT carries an empty list: HTTP 200, no exception, and the members see no
    commands at all. Proved against the live API on 2026-10-16 (EDGE_CASES.md).
    """
    sent: dict[str, object] = {}

    async def fake_guild_upsert(application_id: int, guild_id: int, payload: list[dict]) -> list:
        sent["guild_id"] = guild_id
        sent["names"] = [command["name"] for command in payload]
        return []

    settings = _settings()
    client = build_client(settings)
    _wire(client, monkeypatch, "bulk_upsert_guild_commands", fake_guild_upsert)

    await sync_commands(client, client.tree, settings)
    await client.rag_client.aclose()

    assert sent["guild_id"] == 42
    assert sent["names"] == ["ask", "reset", "status"]


async def test_without_a_guild_the_same_commands_are_registered_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1.1's other half: no `DISCORD_GUILD_ID` means the global endpoint."""
    sent: dict[str, object] = {}

    async def fake_global_upsert(application_id: int, payload: list[dict]) -> list:
        sent["names"] = [command["name"] for command in payload]
        return []

    settings = _settings(guild_id=None, member_role_id=None)
    client = build_client(settings)
    _wire(client, monkeypatch, "bulk_upsert_global_commands", fake_global_upsert)

    await sync_commands(client, client.tree, settings)
    await client.rag_client.aclose()

    assert sent["names"] == ["ask", "reset", "status"]


async def test_a_reconnect_does_not_re_register_the_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`on_ready` fires again on every re-identify; the guard keeps the PUT a one-shot."""
    calls = 0

    async def fake_guild_upsert(application_id: int, guild_id: int, payload: list[dict]) -> list:
        nonlocal calls
        calls += 1
        return []

    settings = _settings()
    client = build_client(settings)
    _wire(client, monkeypatch, "bulk_upsert_guild_commands", fake_guild_upsert)

    await sync_commands(client, client.tree, settings)
    await sync_commands(client, client.tree, settings)
    await client.rag_client.aclose()

    assert calls == 1


def test_to_embed_maps_description_fields_and_footer() -> None:
    embed = to_embed(
        EmbedSpec(
            description="body",
            fields=(FieldSpec(name="Club rules", value="We meet on Tuesdays."),),
            footer="grounded · 2 LLM call(s)",
            colour=1,
        )
    )
    assert embed is not None
    assert embed.description == "body"
    assert embed.fields[0].name == "Club rules"
    assert embed.footer.text == "grounded · 2 LLM call(s)"
    assert to_embed(None) is None


def test_is_member_needs_the_configured_guild_and_role() -> None:
    settings = _settings()
    member_guild = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=42)
    other_guild = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=43)
    other_role = FakeInteraction(user=FakeUser(roles=(FakeRole(8),)), guild_id=42)
    no_roles = FakeInteraction(user=FakeUser(), guild_id=42)

    assert is_member(member_guild, settings) is True
    assert is_member(other_guild, settings) is False
    assert is_member(other_role, settings) is False
    assert is_member(no_roles, settings) is False


def test_is_member_is_false_when_nothing_is_configured() -> None:
    member = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=42)
    assert is_member(member, _settings(guild_id=None)) is False
    assert is_member(member, _settings(member_role_id=None)) is False


async def test_defer_happens_before_the_api_call() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction(events=client.events)

    await handle_ask(interaction, _request(), **_runtime(client))

    assert client.events.index("defer") < client.events.index("ask")
    assert interaction.response.deferred is True


async def test_a_single_part_answer_edits_with_the_buttons() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(), **_runtime(client))

    assert len(interaction.response.edits) == 1
    edit = interaction.response.edits[0]
    assert edit["embed"].description == ANSWER_TEXT
    assert edit["content"] == ""
    assert "view" in edit
    assert interaction.followup.sent == []


async def test_a_long_answer_edits_then_follows_up_with_the_buttons_last() -> None:
    client = FakeRagClient(ChatOutcome(response=chat_response(answer=LONG_ANSWER)))
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(), **_runtime(client))

    assert len(interaction.response.edits) == 1
    assert "view" not in interaction.response.edits[0]
    assert len(interaction.followup.sent) > 0
    for part in interaction.followup.sent[:-1]:
        assert "view" not in part
    assert "view" in interaction.followup.sent[-1]
    assert all(part["ephemeral"] is True for part in interaction.followup.sent)


async def test_public_answers_are_not_ephemeral() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(public=True), **_runtime(client))

    assert interaction.response.deferred is False
    assert interaction.response.edits[0]["content"] == "Sam asked:"


async def test_mentions_are_always_suppressed() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction()
    await handle_ask(interaction, _request(public=True), **_runtime(client))
    assert interaction.response.edits[0]["allowed_mentions"].everyone is False


async def test_an_over_long_question_is_refused_without_a_call() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(question="x" * 2001), **_runtime(client))

    assert client.asks == []
    assert interaction.response.deferred is None
    assert len(interaction.response.sent) == 1
    assert interaction.response.sent[0][1] is True
    assert interaction.response.edits == []


async def test_a_cooldown_refusal_never_reaches_the_api() -> None:
    client = FakeRagClient()
    runtime = _runtime(client, cooldown_s=20)
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(), **runtime)
    await handle_ask(interaction, _request(), **runtime)

    assert len(client.asks) == 1
    assert len(interaction.response.sent) == 1
    assert runtime["slots"].in_flight(42) == 0


async def test_a_401_becomes_one_ephemeral_notice_and_is_not_retried() -> None:
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("unauthorized", 401)))
    interaction = FakeInteraction()

    await handle_ask(interaction, _request(), **_runtime(client))

    assert len(client.asks) == 1
    assert interaction.response.edits[0]["content"] == notice(UNAUTHORIZED, "en")
    assert interaction.response.edits[0]["embed"] is None
    assert interaction.followup.sent == []


async def test_the_slot_is_released_even_when_the_call_fails() -> None:
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("index_unavailable", 503)))
    runtime = _runtime(client)

    await handle_ask(FakeInteraction(), _request(), **runtime)

    assert runtime["slots"].in_flight(42) == 0


async def test_an_expired_interaction_never_publishes_a_private_answer() -> None:
    interaction = ExpiringInteraction()

    await handle_ask(interaction, _request(), **_runtime(FakeRagClient()))

    assert len(interaction.channel.sent) == 1
    assert interaction.channel.sent[0]["content"] == notice(ANSWER_UNDELIVERED, "en")
    assert ANSWER_TEXT not in interaction.channel.sent[0]["content"]


async def test_an_expired_interaction_posts_a_public_answer() -> None:
    interaction = ExpiringInteraction()

    await handle_ask(interaction, _request(public=True), **_runtime(FakeRagClient()))

    assert len(interaction.channel.sent) == 1
    assert interaction.channel.sent[0]["content"] == "Sam asked:"
    assert ANSWER_TEXT in interaction.channel.sent[0]["embed"].description


async def test_reset_answers_ephemerally_and_forgets_the_channel() -> None:
    conversations = ConversationDirectory()
    conversations.remember(42, 99, "c_abc")
    interaction = FakeInteraction()

    await handle_reset(interaction, conversations=conversations)

    assert conversations.get(42, 99) is None
    assert interaction.response.sent[0][1] is True


async def test_status_answers_ephemerally() -> None:
    interaction = FakeInteraction()
    await handle_status(interaction, client=FakeRagClient(), settings=_settings())
    assert interaction.response.sent[0][1] is True
    assert "ok" in interaction.response.sent[0][0]


async def test_one_log_event_per_interaction_without_the_question_the_answer_or_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response().model_dump())

    client = RagClient(
        "http://api:8000",
        timeout_s=5.0,
        member_token=SECRET,
        transport=httpx.MockTransport(handler),
    )
    interaction = FakeInteraction()

    with capture_logs() as entries:
        await handle_ask(interaction, _request(as_member=True), **_runtime(client))  # type: ignore[arg-type]
    await client.aclose()

    done = [entry for entry in entries if entry["event"] == "bot_ask_done"]
    assert len(done) == 1
    assert done[0]["grounded"] is True
    assert done[0]["llm_calls"] == 2
    assert done[0]["channel_id"] == 99
    assert done[0]["latency_ms"] >= 0

    dumped = json.dumps(entries, ensure_ascii=False)
    assert MARKER not in dumped
    assert ANSWER_TEXT not in dumped
    assert SECRET not in dumped


async def test_a_failure_logs_its_code_and_nothing_else() -> None:
    client = FakeRagClient(ChatOutcome(failure=ApiFailure("rate_limited", 429)))

    with capture_logs() as entries:
        await handle_ask(FakeInteraction(), _request(), **_runtime(client))

    failed = [entry for entry in entries if entry["event"] == "bot_ask_failed"]
    assert len(failed) == 1
    assert failed[0]["code"] == "rate_limited"
    assert failed[0]["status"] == 429
    assert MARKER not in json.dumps(entries, ensure_ascii=False)


async def test_a_rejected_ask_logs_the_rejection() -> None:
    client = FakeRagClient()

    with capture_logs() as entries:
        await handle_ask(FakeInteraction(), _request(question="x" * 2001), **_runtime(client))

    assert [entry["event"] for entry in entries] == ["bot_ask_rejected"]
    assert MARKER not in json.dumps(entries, ensure_ascii=False)


def test_structlog_is_the_logger() -> None:
    """Req 7.5: the bot logs through structlog like the app, so `capture_logs` sees every event."""
    assert isinstance(structlog.get_logger("mcclub_rag.discordbot.app"), object)
