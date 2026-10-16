"""The Discord wiring: defer order, delivery, roles, expiry, logging (Req 1, 3, 7, 11-15)."""

import asyncio
import json

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from mcclub_rag.discordbot.app import (
    _IN_FLIGHT_INGESTS,
    build_client,
    handle_ask,
    handle_documents,
    handle_forget,
    handle_ingest,
    handle_reset,
    handle_status,
    interaction_language,
    is_admin,
    is_member,
    run_ingest_task,
    sync_commands,
    to_embed,
)
from mcclub_rag.discordbot.client import AdminOutcome, ApiFailure, ChatOutcome, RagClient
from mcclub_rag.discordbot.commands import AskRequest, IngestRequest
from mcclub_rag.discordbot.notices import (
    ADMIN_ONLY,
    ANSWER_UNDELIVERED,
    INGEST_DONE,
    INGEST_STARTED,
    ONE_SOURCE,
    UNAUTHORIZED,
    notice,
)
from mcclub_rag.discordbot.render import EmbedSpec, FieldSpec
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from mcclub_rag.discordbot.views import DeleteView
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
    values: dict[str, object] = {"bot_token": "test-token", "guilds": "42:7"}
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


EXPECTED_COMMANDS = ["ask", "reset", "status", "documents", "ingest", "forget"]


def _wire(client: object, monkeypatch: pytest.MonkeyPatch, method: str, fake) -> None:  # noqa: ANN001
    """Capture the payload discord.py would put on the wire, without a connection."""
    # The tree refuses to sync without an application id, which normally arrives with READY.
    client._connection.application_id = 1560638956083609730  # noqa: SLF001 - test seam
    monkeypatch.setattr(client.http, method, fake)


async def test_a_configured_guild_receives_the_commands_in_the_synced_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1.1: with a guild configured, every command must reach that guild.

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
    assert sorted(sent["names"]) == sorted(EXPECTED_COMMANDS)


async def test_without_a_guild_the_same_commands_are_registered_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 1.1's other half: no guild configured means the global endpoint."""
    sent: dict[str, object] = {}

    async def fake_global_upsert(application_id: int, payload: list[dict]) -> list:
        sent["names"] = [command["name"] for command in payload]
        return []

    settings = _settings(guilds="")
    client = build_client(settings)
    _wire(client, monkeypatch, "bulk_upsert_global_commands", fake_global_upsert)

    await sync_commands(client, client.tree, settings)
    await client.rag_client.aclose()

    assert sorted(sent["names"]) == sorted(EXPECTED_COMMANDS)


async def test_every_configured_guild_is_registered_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 16.5: one server must not be left running a stale command set (or none at all)."""
    guilds: list[int] = []

    async def fake_guild_upsert(application_id: int, guild_id: int, payload: list[dict]) -> list:
        guilds.append(guild_id)
        return []

    settings = _settings(guilds="42:7:8,99:6:9")
    client = build_client(settings)
    _wire(client, monkeypatch, "bulk_upsert_guild_commands", fake_guild_upsert)

    await sync_commands(client, client.tree, settings)
    await client.rag_client.aclose()

    assert guilds == [42, 99]


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
    settings = _settings()  # guild 42 grants member role 7
    member_guild = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=42)
    other_guild = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=43)
    other_role = FakeInteraction(user=FakeUser(roles=(FakeRole(8),)), guild_id=42)
    no_roles = FakeInteraction(user=FakeUser(), guild_id=42)

    assert is_member(member_guild, settings) is True
    assert is_member(other_guild, settings) is False
    assert is_member(other_role, settings) is False
    assert is_member(no_roles, settings) is False


def test_is_member_is_false_when_nothing_is_configured() -> None:
    """Req 16.4: the same person in a guild that grants nothing is a public caller."""
    member = FakeInteraction(user=FakeUser(roles=(FakeRole(7),)), guild_id=42)
    assert is_member(member, _settings(guilds="")) is False
    assert is_member(member, _settings(guilds="42")) is False  # listed, no member role
    assert is_member(member, _settings(guilds="43:7")) is False  # another guild's role id


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


# --- The admin commands (requirements 11 to 15) ----------------------------------------------


def _admin() -> FakeUser:
    """An operator holding the admin role of guild 42."""
    return FakeUser(roles=(FakeRole(9),))


def _admin_settings(**overrides: object) -> BotSettings:
    """The default settings with an admin role on guild 42 (Req 11.1).

    The member role stays configured, so a test that passes here cannot be passing because guild
    42 granted nothing at all.
    """
    return _settings(guilds="42:7:9", **overrides)


def _ingest_request(**overrides: object) -> IngestRequest:
    values: dict[str, object] = {
        "filename": "rules.pdf",
        "data": b"%PDF-1.7 bytes",
        "url": None,
        "visibility": "public",
        "topic": None,
        "user_id": 7,
        "guild_id": 42,
        "channel_id": 99,
    }
    values.update(overrides)
    return IngestRequest(**values)  # type: ignore[arg-type]


async def _finish_ingests() -> None:
    """Await the background tasks `handle_ingest` spawned, as the bot's loop would."""
    for _ in range(5):
        if not _IN_FLIGHT_INGESTS:
            return
        await asyncio.gather(*list(_IN_FLIGHT_INGESTS))


def test_admin_rights_come_from_the_guilds_own_entry() -> None:
    """Req 11.1, 16.4: the role must be the one this guild granted, and the guild comes first."""
    settings = _settings(guilds="42::9,43:7")
    assert is_admin(FakeInteraction(user=_admin()), settings) is True
    assert is_admin(FakeInteraction(user=_admin(), guild_id=43), settings) is False
    assert is_admin(FakeInteraction(user=_admin(), guild_id=99), settings) is False
    assert is_admin(FakeInteraction(user=_admin(), guild_id=None), settings) is False


def test_the_member_role_is_never_an_admin_role() -> None:
    """Req 11.2: no fallback to the member role, and no fallback to Discord's permissions."""
    settings = _settings(guilds="42:7")
    assert is_member(FakeInteraction(user=FakeUser(roles=(FakeRole(7),))), settings) is True
    assert is_admin(FakeInteraction(user=FakeUser(roles=(FakeRole(7),))), settings) is False


def test_nothing_is_admin_when_no_guild_is_configured() -> None:
    assert is_admin(FakeInteraction(user=_admin()), _settings(guilds="")) is False


@pytest.mark.parametrize(
    ("locale", "language"),
    [("fr", "fr"), ("fr-FR", "fr"), ("ar", "ar"), ("de-DE", "en"), ("", "en")],
)
def test_the_notice_language_comes_from_the_operators_own_discord(
    locale: str, language: str
) -> None:
    """Req 12.4: an admin command carries no question, so there is nothing else to detect."""
    assert interaction_language(FakeInteraction(locale=locale)) == language


async def test_documents_answers_ephemerally_with_the_listing() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction(user=_admin())

    await handle_documents(interaction, client=client, settings=_admin_settings())

    assert client.list_calls == 1
    sent = interaction.response.raw[0]
    assert sent["ephemeral"] is True
    assert sent["embed"].description.startswith("**Club rules**")
    assert interaction.response.deferred is None


@pytest.mark.parametrize(
    "call",
    [
        lambda i, c, s: handle_documents(i, client=c, settings=s),
        lambda i, c, s: handle_ingest(i, _ingest_request(), client=c, settings=s),
        lambda i, c, s: handle_forget(i, "doc-1", client=c, settings=s),
    ],
)
async def test_a_non_admin_is_refused_before_any_api_call(call) -> None:  # noqa: ANN001
    """Req 11.1, 11.3: one ephemeral notice, no call, and no hint about the API or the key."""
    client = FakeRagClient()
    interaction = FakeInteraction(user=FakeUser(roles=(FakeRole(8),)))

    await call(interaction, client, _settings())

    assert interaction.response.sent == [(notice(ADMIN_ONLY, "en"), True)]
    assert client.list_calls == 0 and client.ingests == [] and client.deletes == []
    assert interaction.response.deferred is None


async def test_a_refusal_is_logged_without_the_corpus_or_the_key() -> None:
    with capture_logs() as entries:
        await handle_documents(
            FakeInteraction(user=FakeUser(roles=())),
            client=FakeRagClient(),
            settings=_admin_settings(),
        )
    rejected = [entry for entry in entries if entry["event"] == "bot_admin_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["command"] == "documents"
    assert rejected[0]["guild_id"] == 42
    assert SECRET not in json.dumps(entries, default=str)


async def test_an_ingest_of_neither_source_is_refused_without_deferring() -> None:
    """Req 13.1: no acknowledgement, because there is nothing to work on."""
    client = FakeRagClient()
    interaction = FakeInteraction(user=_admin())

    await handle_ingest(
        interaction,
        _ingest_request(filename=None, data=None),
        client=client,
        settings=_admin_settings(),
    )

    assert interaction.response.sent == [(notice(ONE_SOURCE, "en"), True)]
    assert interaction.response.deferred is None
    assert client.ingests == []


async def test_an_ingest_acknowledges_then_edits_its_own_message() -> None:
    """Req 13.3: the defer and the acknowledgement happen before the minutes-long ingest."""
    client = FakeRagClient()
    interaction = FakeInteraction(events=client.events, user=_admin())

    await handle_ingest(interaction, _ingest_request(), client=client, settings=_admin_settings())

    assert interaction.response.deferred is True
    acknowledgement = notice(INGEST_STARTED, "en", name="rules.pdf", visibility="public")
    assert interaction.response.edits[0]["content"] == acknowledgement

    await _finish_ingests()

    # The defer and the acknowledgement came first: the work had not started when they were sent.
    assert client.events.index("defer") < client.events.index("ingest_file")
    assert interaction.response.edits[0]["content"] == acknowledgement
    final = interaction.response.edits[-1]
    assert final["content"] == notice(
        INGEST_DONE, "en", name="rules.pdf", visibility="public", doc_id="doc-new"
    )
    assert final["view"] is None


async def test_an_expired_interaction_gets_the_result_in_the_channel() -> None:
    """Req 13.3: an ingest outlives the 15-minute token, so the outcome must still arrive."""
    client = FakeRagClient()
    interaction = ExpiringInteraction(events=client.events, user=_admin())

    await run_ingest_task(interaction, _ingest_request(), client=client, language="en")

    assert interaction.response.edits == []
    assert interaction.channel.sent[0]["content"] == notice(
        INGEST_DONE, "en", name="rules.pdf", visibility="public", doc_id="doc-new"
    )


async def test_an_undeliverable_ingest_result_is_logged_not_lost() -> None:
    client = FakeRagClient()
    interaction = ExpiringInteraction(events=client.events, user=_admin())
    interaction.channel = None

    with capture_logs() as entries:
        await run_ingest_task(interaction, _ingest_request(), client=client, language="en")

    events = [entry["event"] for entry in entries]
    assert "bot_ingest_undelivered" in events
    assert "bot_ingest_failed" not in events  # the ingest worked; only the delivery did not


async def test_every_ingest_outcome_is_logged_with_the_id() -> None:
    client = FakeRagClient()
    interaction = FakeInteraction(user=_admin(), events=client.events)

    with capture_logs() as entries:
        await run_ingest_task(interaction, _ingest_request(), client=client, language="en")

    done = [entry for entry in entries if entry["event"] == "bot_ingest_done"]
    assert len(done) == 1
    assert done[0]["doc_id"] == "doc-new"
    assert MARKER not in json.dumps(entries, default=str)


async def test_a_failed_ingest_is_logged_with_its_code() -> None:
    client = FakeRagClient(admin=AdminOutcome(failure=ApiFailure("parse_error", 422)))
    interaction = FakeInteraction(user=_admin(), events=client.events)

    with capture_logs() as entries:
        await run_ingest_task(interaction, _ingest_request(), client=client, language="en")

    failed = [entry for entry in entries if entry["event"] == "bot_ingest_failed"]
    assert len(failed) == 1
    assert failed[0]["code"] == "parse_error"


async def test_forget_asks_for_a_press_before_deleting() -> None:
    """Req 14.1: no `DELETE` reaches the API from the command itself."""
    client = FakeRagClient()
    interaction = FakeInteraction(user=_admin())

    await handle_forget(interaction, "doc-1", client=client, settings=_admin_settings())

    assert client.deletes == []
    sent = interaction.response.raw[0]
    assert sent["ephemeral"] is True
    assert "doc-1" in sent["content"]
    assert isinstance(sent["view"], DeleteView)
