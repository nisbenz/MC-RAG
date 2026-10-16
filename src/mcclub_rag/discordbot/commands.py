"""The three commands' decisions, in plain Python: no discord.py and no httpx.

`precheck` is synchronous so the Discord layer can refuse an over-long question or a caller inside
their cooldown *before* deferring, spending neither an interaction nor an API call. `run_ask` makes
exactly one chat call and never retries (Req 6.3).
"""

from dataclasses import dataclass
from math import ceil

from mcclub_rag.discordbot.client import ApiFailure, RagClient
from mcclub_rag.discordbot.notices import (
    BUSY,
    COOLDOWN,
    RATE_LIMITED,
    RESET_DONE,
    SERVICE_UNAVAILABLE,
    STATUS,
    TOO_LONG,
    UNAUTHORIZED,
    NoticeLanguage,
    notice,
    notice_language,
)
from mcclub_rag.discordbot.render import RenderedReply, notice_reply, render_answer
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown

MAX_QUESTION_CHARS = 2000  # the API's own pydantic cap; a longer body would be a 422

_FAILURE_TABLES: dict[str, dict[str, str]] = {
    "unauthorized": UNAUTHORIZED,
    "rate_limited": RATE_LIMITED,
}


@dataclass(frozen=True)
class AskOutcome:
    """The reply plus what the caller needs to log. Keeps logging in the Discord layer."""

    reply: RenderedReply
    failure: ApiFailure | None = None
    grounded: bool | None = None
    llm_calls: int | None = None


@dataclass(frozen=True)
class AskRequest:
    question: str
    public: bool
    user_id: int
    asker: str
    guild_id: int | None
    channel_id: int
    as_member: bool


def precheck(
    request: AskRequest,
    *,
    cooldown: UserCooldown,
    slots: GuildSlots,
) -> RenderedReply | None:
    """A refusal to send instead of asking, or `None` once a slot and the cooldown are reserved.

    The slot is taken first so a refused cooldown can hand it straight back, and so the two guards
    cannot both half-apply.
    """
    language = notice_language(request.question)
    if len(request.question) > MAX_QUESTION_CHARS:
        return notice_reply(notice(TOO_LONG, language))
    if not slots.acquire(request.guild_id):
        return notice_reply(notice(BUSY, language))
    remaining = cooldown.acquire(request.user_id)
    if remaining > 0:
        slots.release(request.guild_id)
        return notice_reply(notice(COOLDOWN, language, seconds=ceil(remaining)))
    return None


async def run_ask(
    request: AskRequest,
    *,
    client: RagClient,
    conversations: ConversationDirectory,
) -> AskOutcome:
    """One chat call. A blank question is forwarded untouched and answered by the API (Req 1.5)."""
    language = notice_language(request.question)
    outcome = await client.ask(
        request.question,
        as_member=request.as_member,
        conversation_id=conversations.get(request.guild_id, request.channel_id),
    )
    if outcome.failure is not None or outcome.response is None:
        failure = outcome.failure or ApiFailure("internal")
        return AskOutcome(reply=failure_reply(failure, language), failure=failure)
    response = outcome.response
    conversations.remember(request.guild_id, request.channel_id, response.conversation_id)
    return AskOutcome(
        reply=render_answer(
            response, public=request.public, asker=request.asker, language=language
        ),
        grounded=response.grounded,
        llm_calls=response.usage.llm_calls,
    )


def run_reset(
    guild_id: int | None,
    channel_id: int,
    conversations: ConversationDirectory,
    *,
    language: NoticeLanguage = "en",
) -> RenderedReply:
    """Start a new conversation here by dropping the cached id; the old one expires on its own."""
    conversations.forget(guild_id, channel_id)
    return notice_reply(notice(RESET_DONE, language))


async def run_status(
    *,
    client: RagClient,
    settings: BotSettings,
    language: NoticeLanguage = "en",
) -> RenderedReply:
    """API health and the bot's own guards. `/health` carries no LLM headroom (Req 6.4)."""
    outcome = await client.health()
    if outcome.failure is not None:
        return failure_reply(outcome.failure, language)
    return notice_reply(
        notice(
            STATUS,
            language,
            status=outcome.status,
            documents=outcome.documents,
            cooldown=f"{settings.user_cooldown_s:g}",
            concurrency=settings.max_concurrency,
        )
    )


def failure_reply(failure: ApiFailure, language: NoticeLanguage) -> RenderedReply:
    """Every API failure becomes one ephemeral notice in the question's language (Req 7.2)."""
    table = _FAILURE_TABLES.get(failure.code, SERVICE_UNAVAILABLE)
    return notice_reply(notice(table, language))
