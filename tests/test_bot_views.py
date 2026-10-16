"""The feedback buttons and the delete confirmation (Req 5, 14)."""

import discord
from structlog.testing import capture_logs

from mcclub_rag.discordbot.client import AdminOutcome, ApiFailure
from mcclub_rag.discordbot.notices import (
    DELETE_CANCELLED,
    DELETE_DONE,
    DELETE_NOT_FOUND,
    SERVICE_UNAVAILABLE,
    notice,
)
from mcclub_rag.discordbot.views import (
    CONFIRM_TIMEOUT_S,
    DOWN_LABEL,
    FEEDBACK_PREFIX,
    UP_LABEL,
    DeleteView,
    FeedbackView,
    custom_id,
    delete_custom_id,
    parse_custom_id,
    parse_delete_custom_id,
    submit_rating,
)
from tests.bot_fakes import FakeInteraction, FakeRagClient

CONVERSATION_ID = "c_1e4f2a"
DOC_ID = "doc-1e4f2a"


def _view(client: FakeRagClient | None = None) -> FeedbackView:
    return FeedbackView(CONVERSATION_ID, client=client, language="en")


def test_custom_id_round_trips() -> None:
    for rating in ("up", "down"):
        value = custom_id(CONVERSATION_ID, rating)  # type: ignore[arg-type]
        press = parse_custom_id(value)
        assert press is not None
        assert press.conversation_id == CONVERSATION_ID
        assert press.rating == rating


def test_custom_id_stays_inside_discords_limit() -> None:
    value = custom_id("c_" + "0" * 36, "up")
    assert len(value) <= 100


def test_parse_rejects_everything_that_is_not_ours() -> None:
    assert parse_custom_id("") is None
    assert parse_custom_id("other:fb:up:c_1") is None
    assert parse_custom_id(f"{FEEDBACK_PREFIX}:sideways:{CONVERSATION_ID}") is None
    assert parse_custom_id(f"{FEEDBACK_PREFIX}:up:") is None
    assert parse_custom_id(f"{FEEDBACK_PREFIX}:up") is None


def test_view_has_both_buttons_with_our_ids() -> None:
    view = _view()
    assert [item.custom_id for item in view.children] == [
        custom_id(CONVERSATION_ID, "up"),
        custom_id(CONVERSATION_ID, "down"),
    ]
    assert [item.label for item in view.children] == [UP_LABEL, DOWN_LABEL]
    assert view.timeout is None


async def test_a_press_records_once_and_leaves_one_disabled_button() -> None:
    client = FakeRagClient()
    view = _view(client)
    interaction = FakeInteraction()
    button = view.children[0]

    await view.record(interaction, button)

    assert client.ratings == [(CONVERSATION_ID, "up")]
    assert len(view.children) == 1
    assert view.children[0].disabled is True
    assert view.children[0].custom_id == custom_id(CONVERSATION_ID, "up")
    assert interaction.events == ["edit_message", "followup"]


async def test_the_down_button_records_down() -> None:
    client = FakeRagClient()
    view = _view(client)
    await view.record(FakeInteraction(), view.children[1])
    assert client.ratings == [(CONVERSATION_ID, "down")]
    assert view.children[0].custom_id == custom_id(CONVERSATION_ID, "down")


async def test_a_failed_recording_still_answers_the_member() -> None:
    client = FakeRagClient(feedback_ok=False)
    view = _view(client)
    interaction = FakeInteraction()
    await view.record(interaction, view.children[0])
    assert client.ratings == [(CONVERSATION_ID, "up")]
    assert interaction.followup.sent[0]["content"] == "I could not record that rating."


async def test_an_already_answered_interaction_gets_a_followup_not_an_edit() -> None:
    client = FakeRagClient()
    view = _view(client)
    interaction = FakeInteraction()
    await interaction.response.send_message("(already answered)", ephemeral=True)

    await view.record(interaction, view.children[0])

    assert interaction.response.edits == []
    assert interaction.followup.sent[0]["content"] == "Thanks, that rating was recorded."


async def test_submit_rating_calls_the_client_once() -> None:
    client = FakeRagClient()
    press = parse_custom_id(custom_id(CONVERSATION_ID, "down"))
    assert press is not None
    assert await submit_rating(client, press) is True
    assert client.ratings == [(CONVERSATION_ID, "down")]


def test_buttons_are_plain_components() -> None:
    """No callback is registered on `add_view`, so a persistent view cannot double-count."""
    view = _view()
    assert all(isinstance(item, discord.ui.Button) for item in view.children)
    assert all(not item.disabled for item in view.children)


async def test_a_failed_recording_is_logged() -> None:
    client = FakeRagClient(feedback_ok=False)
    view = _view(client)

    with capture_logs() as entries:
        await view.record(FakeInteraction(), view.children[0])

    failed = [entry for entry in entries if entry["event"] == "bot_feedback_failed"]
    assert len(failed) == 1
    assert failed[0]["rating"] == "up"


# --- The delete confirmation (requirements 14.1, 14.2) -----------------------------------------


def _delete_view(client: FakeRagClient | None = None) -> DeleteView:
    return DeleteView(DOC_ID, client=client, language="en")


def test_a_delete_press_cannot_delete_something_the_prompt_did_not_name() -> None:
    view = _delete_view()
    assert [item.custom_id for item in view.children] == [
        delete_custom_id(DOC_ID),
        delete_custom_id(DOC_ID),
    ]
    assert view.children[0].style is discord.ButtonStyle.danger
    assert view.children[1].style is discord.ButtonStyle.secondary
    assert parse_delete_custom_id("other:" + DOC_ID) is None
    assert parse_delete_custom_id(delete_custom_id("")) is None


def test_the_confirmation_expires() -> None:
    """Req 14.2: a stale prompt must not delete something else later."""
    assert _delete_view().timeout == CONFIRM_TIMEOUT_S == 120


async def test_confirming_deletes_exactly_the_named_id() -> None:
    client = FakeRagClient(admin=AdminOutcome(deleted=True))
    view = _delete_view(client)
    interaction = FakeInteraction()

    await view.press(interaction, confirmed=True)

    assert client.deletes == [DOC_ID]
    assert interaction.events == ["defer", "edit_original_response"]
    assert interaction.response.edits[0]["content"] == notice(DELETE_DONE, "en", doc_id=DOC_ID)
    assert interaction.response.edits[0]["view"] is None
    assert view.is_finished()


async def test_cancelling_calls_nothing() -> None:
    client = FakeRagClient()
    view = _delete_view(client)
    interaction = FakeInteraction()

    await view.press(interaction, confirmed=False)

    assert client.deletes == []
    assert interaction.response.edits[0]["content"] == notice(DELETE_CANCELLED, "en")


async def test_a_missing_document_is_reported_as_missing_after_the_press() -> None:
    """Req 14.3: `{"ok": false}` is not success, even though a button was pressed."""
    client = FakeRagClient(admin=AdminOutcome(deleted=False))
    interaction = FakeInteraction()

    await _delete_view(client).press(interaction, confirmed=True)

    assert interaction.response.edits[0]["content"] == notice(DELETE_NOT_FOUND, "en", doc_id=DOC_ID)


async def test_a_failed_delete_says_so() -> None:
    client = FakeRagClient(admin=AdminOutcome(failure=ApiFailure("unreachable")))
    interaction = FakeInteraction()

    await _delete_view(client).press(interaction, confirmed=True)

    assert interaction.response.edits[0]["content"] == notice(SERVICE_UNAVAILABLE, "en")


async def test_a_delete_without_a_client_deletes_nothing_and_says_nothing() -> None:
    interaction = FakeInteraction()

    with capture_logs() as entries:
        await _delete_view(None).press(interaction, confirmed=True)

    assert interaction.response.edits == []
    assert [entry["event"] for entry in entries] == ["bot_delete_without_client"]
