"""The three commands' decisions, in plain Python: no discord.py and no httpx.

`precheck` is synchronous so the Discord layer can refuse an over-long question or a caller inside
their cooldown *before* deferring, spending neither an interaction nor an API call. `run_ask` makes
exactly one chat call and never retries (Req 6.3).
"""

from dataclasses import dataclass
from math import ceil

from mcclub_rag.discordbot.client import ApiFailure, RagClient
from mcclub_rag.discordbot.notices import (
    ADMIN_REFUSED,
    BUSY,
    COOLDOWN,
    DELETE_CONFIRM,
    INGEST_STARTED,
    ONE_SOURCE,
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
from mcclub_rag.discordbot.render import (
    RenderedReply,
    delete_result_reply,
    ingest_failure_reply,
    notice_reply,
    render_answer,
    render_documents,
    render_ingest,
)
from mcclub_rag.discordbot.settings import BotSettings
from mcclub_rag.discordbot.state import ConversationDirectory, GuildSlots, UserCooldown
from mcclub_rag.schemas import Visibility

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


# --- The admin commands (requirements 11 to 14) ----------------------------------------------

_ADMIN_FAILURE_TABLES: dict[str, dict[str, str]] = {
    "unauthorized": ADMIN_REFUSED,
    "forbidden": ADMIN_REFUSED,
    "rate_limited": RATE_LIMITED,
}


@dataclass(frozen=True)
class IngestRequest:
    """One `/ingest` call, decided before any network use (Req 13.1, 13.2)."""

    filename: str | None
    data: bytes | None
    url: str | None
    visibility: Visibility
    topic: str | None
    user_id: int
    guild_id: int | None
    channel_id: int

    @property
    def name(self) -> str:
        """What the operator will recognise in the reply."""
        return self.filename or self.url or "document"


@dataclass(frozen=True)
class IngestOutcome:
    """The reply plus what the Discord layer needs to log, as `AskOutcome` does."""

    reply: RenderedReply
    failure: ApiFailure | None = None
    doc_id: str | None = None
    name: str = "document"


def ingest_precheck(request: IngestRequest, *, language: NoticeLanguage) -> RenderedReply | None:
    """Refuse both sources and neither before any API call, like `precheck` (Req 13.1)."""
    has_file = request.filename is not None and request.data is not None
    if has_file == bool(request.url):
        return notice_reply(notice(ONE_SOURCE, language))
    return None


def ingest_started_reply(request: IngestRequest, *, language: NoticeLanguage) -> RenderedReply:
    """The acknowledgement, edited later or posted to the channel (Req 13.3)."""
    return notice_reply(
        notice(INGEST_STARTED, language, name=request.name, visibility=request.visibility)
    )


def forget_confirmation(
    *, doc_id: str, title: str | None = None, language: NoticeLanguage = "en"
) -> RenderedReply:
    """The prompt the confirmation view rides on; it names the document, never just the id."""
    return notice_reply(notice(DELETE_CONFIRM, language, name=title or doc_id, doc_id=doc_id))


def admin_failure_reply(failure: ApiFailure, language: NoticeLanguage) -> RenderedReply:
    """An admin call that failed: a refused key says so, everything else is the shared notice."""
    table = _ADMIN_FAILURE_TABLES.get(failure.code)
    if table is None:
        return failure_reply(failure, language)
    return notice_reply(notice(table, language))


async def run_documents(*, client: RagClient, language: NoticeLanguage = "en") -> RenderedReply:
    """The corpus listing (Req 12)."""
    outcome = await client.list_documents()
    if outcome.failure is not None or outcome.documents is None:
        return admin_failure_reply(outcome.failure or ApiFailure("internal"), language)
    return render_documents(outcome.documents, language=language)


async def run_ingest(
    request: IngestRequest,
    *,
    client: RagClient,
    language: NoticeLanguage = "en",
) -> IngestOutcome:
    """One ingest call, never retried (Req 13.5)."""
    if request.url is not None:
        outcome = await client.ingest_url(
            request.url, visibility=request.visibility, topic=request.topic
        )
    else:
        outcome = await client.ingest_file(
            request.name,
            request.data or b"",
            visibility=request.visibility,
            topic=request.topic,
        )
    if outcome.failure is not None or outcome.doc_id is None:
        failure = outcome.failure or ApiFailure("internal")
        # A refused key is an operator problem and says so; anything else is this document's
        # failure, named with the API's own code so the cause is not guessed at.
        if failure.code in ("unauthorized", "forbidden"):
            reply = admin_failure_reply(failure, language)
        else:
            reply = ingest_failure_reply(request.name, failure.code, language=language)
        return IngestOutcome(reply=reply, failure=failure, name=request.name)
    return IngestOutcome(
        reply=render_ingest(
            name=request.name,
            visibility=request.visibility,
            doc_id=outcome.doc_id,
            language=language,
        ),
        doc_id=outcome.doc_id,
        name=request.name,
    )


async def run_forget(
    *, client: RagClient, doc_id: str, language: NoticeLanguage = "en"
) -> RenderedReply:
    """The delete behind the confirmation press (Req 14.1, 14.3)."""
    outcome = await client.delete_document(doc_id)
    if outcome.failure is not None or outcome.deleted is None:
        return admin_failure_reply(outcome.failure or ApiFailure("internal"), language)
    return delete_result_reply(doc_id, outcome.deleted, language=language)
