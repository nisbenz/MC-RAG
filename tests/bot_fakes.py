"""Fakes for the Discord bot tests: no gateway, no API, no model files.

Mirrors `tests/pipeline_helpers.py`: the bot's whole member-visible behaviour is asserted through
`FakeInteraction` and `FakeRagClient`, so nothing here opens a socket.
"""

from typing import Any

import discord

from mcclub_rag.discordbot.client import ChatOutcome, HealthOutcome
from mcclub_rag.schemas import ChatResponse, Source, Usage

ANSWER_TEXT = "The club meets on Tuesdays at the workshop."
SNIPPET_TEXT = "We meet on Tuesdays."


def chat_response(
    answer: str = ANSWER_TEXT,
    *,
    conversation_id: str = "c_test",
    grounded: bool = True,
    sources: list[Source] | None = None,
    llm_calls: int = 2,
) -> ChatResponse:
    if sources is None:
        sources = (
            [Source(doc_id="doc-1", title="Club rules", snippet=SNIPPET_TEXT)] if grounded else []
        )
    return ChatResponse(
        answer=answer,
        conversation_id=conversation_id,
        grounded=grounded,
        sources=sources,
        usage=Usage(llm_calls=llm_calls, prompt_tokens=100, completion_tokens=20),
        latency_ms=1200,
    )


class FakeRagClient:
    """Records what the bot asked for, and returns whatever the test set up."""

    def __init__(
        self,
        outcome: ChatOutcome | None = None,
        *,
        health: HealthOutcome | None = None,
        feedback_ok: bool = True,
        events: list[str] | None = None,
    ) -> None:
        self.outcome = outcome if outcome is not None else ChatOutcome(response=chat_response())
        self.health_outcome = (
            health if health is not None else HealthOutcome(status="ok", documents=60)
        )
        self.feedback_ok = feedback_ok
        self.asks: list[dict[str, Any]] = []
        self.ratings: list[tuple[str, str]] = []
        self.health_calls = 0
        self.events = events if events is not None else []

    async def ask(
        self,
        question: str,
        *,
        as_member: bool,
        conversation_id: str | None = None,
    ) -> ChatOutcome:
        self.asks.append(
            {"question": question, "as_member": as_member, "conversation_id": conversation_id}
        )
        self.events.append("ask")
        return self.outcome

    async def feedback(self, conversation_id: str, rating: str) -> bool:
        self.ratings.append((conversation_id, rating))
        self.events.append("feedback")
        return self.feedback_ok

    async def health(self) -> HealthOutcome:
        self.health_calls += 1
        return self.health_outcome

    async def aclose(self) -> None:
        self.events.append("aclose")


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeUser:
    def __init__(self, *, user_id: int = 7, display_name: str = "Sam", roles: tuple = ()) -> None:
        self.id = user_id
        self.display_name = display_name
        self.roles = roles


class FakeResponse:
    """The interaction's first-response surface: defer, send_message, edit_message."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.deferred: bool | None = None
        self.sent: list[tuple[str | None, bool]] = []
        self.edits: list[dict[str, Any]] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = ephemeral
        self._done = True
        self.events.append("defer")

    async def send_message(
        self, content: str | None = None, *, ephemeral: bool = False, **kwargs: Any
    ) -> None:
        self.sent.append((content, ephemeral))
        self._done = True
        self.events.append("send_message")

    async def edit_message(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        self._done = True
        self.events.append("edit_message")


class FakeFollowup:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if content is not None:
            kwargs["content"] = content
        self.sent.append(kwargs)
        self.events.append("followup")


class FakeChannel:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> None:
        if content is not None:
            kwargs["content"] = content
        self.sent.append(kwargs)
        self.events.append("channel_send")


class FakeInteraction:
    """Everything `app.py` touches on a real interaction, plus an ordering log."""

    def __init__(
        self,
        *,
        user: FakeUser | None = None,
        guild_id: int | None = 42,
        channel_id: int | None = 99,
        events: list[str] | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.user = user if user is not None else FakeUser()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.response = FakeResponse(self.events)
        self.followup = FakeFollowup(self.events)
        self.channel = FakeChannel(self.events)

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.response.edits.append(kwargs)
        self.events.append("edit_original_response")


class FakeHttpResponse:
    """Just enough of a response object for discord.py's HTTPException."""

    status = 404
    reason = "Not Found"


def expired_interaction_error() -> discord.NotFound:
    """What discord.py raises when the interaction token has expired (error code 10062)."""
    return discord.NotFound(
        FakeHttpResponse(),  # type: ignore[arg-type]
        {"message": "Unknown interaction", "code": 10062},
    )
