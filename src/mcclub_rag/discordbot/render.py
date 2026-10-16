"""Turning a `ChatResponse` into Discord-shaped parts (Req 2).

Everything a member sees is decided here, in plain dataclasses, so it is testable without a gateway.
The Discord types only appear in `app.py`, which sends what this module produced.
"""

from dataclasses import dataclass

from mcclub_rag.discordbot.notices import (
    DELETE_DONE,
    DELETE_NOT_FOUND,
    INGEST_DONE,
    INGEST_FAILED,
    NO_DOCUMENTS,
    NOT_GROUNDED,
    NoticeLanguage,
    notice,
)
from mcclub_rag.schemas import ChatResponse, DocInfo, Source, Usage

EMBED_DESCRIPTION_LIMIT = 4000  # Discord's embed description cap
MESSAGE_CONTENT_LIMIT = 1900  # under Discord's 2000 so a `…` marker still fits
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_SOURCE_FIELDS = 10  # Discord allows 25; ten is all a member reads
CONTINUATION_MARK = "\u2026"
SOURCE_FALLBACK = "Source"
GROUNDED_COLOUR = 0x2ECC71
UNGROUNDED_COLOUR = 0x95A5A6
LIST_COLOUR = 0x3498DB


@dataclass(frozen=True)
class FieldSpec:
    name: str
    value: str


@dataclass(frozen=True)
class EmbedSpec:
    description: str
    fields: tuple[FieldSpec, ...] = ()
    footer: str | None = None
    colour: int = GROUNDED_COLOUR


@dataclass(frozen=True)
class Message:
    content: str
    embed: EmbedSpec | None = None
    ephemeral: bool = True


@dataclass(frozen=True)
class RenderedReply:
    """Part 0 edits the deferred response; the rest are follow-ups. `feedback` arms the buttons."""

    parts: tuple[Message, ...]
    feedback: str | None = None


def notice_reply(text: str, *, ephemeral: bool = True) -> RenderedReply:
    """A single-part reply with no embed: a refusal, a notice, a failure."""
    return RenderedReply(parts=(Message(content=text, embed=None, ephemeral=ephemeral),))


def render_answer(
    response: ChatResponse,
    *,
    public: bool,
    asker: str,
    language: NoticeLanguage,
) -> RenderedReply:
    """The whole reply for one answer: grounding, sources, usage, and the split (Req 2.1-2.6)."""
    ephemeral = not public
    description, remainder = _take(response.answer, EMBED_DESCRIPTION_LIMIT)
    if not response.grounded:
        description = f"{notice(NOT_GROUNDED, language)}\n\n{description}".strip()
    continuation = _mark_continuations(split_text(remainder)) if remainder else []
    if continuation:  # part 0 is followed by another, so it carries the marker too (Req 2.4)
        description = f"{description}{CONTINUATION_MARK}"
    total = 1 + len(continuation)
    parts = [
        Message(
            content=attribution(asker) if public else "",
            embed=EmbedSpec(
                description=description,
                fields=source_fields(response.sources),
                footer=usage_footer(response.usage, response.grounded, part=1, total=total),
                colour=GROUNDED_COLOUR if response.grounded else UNGROUNDED_COLOUR,
            ),
            ephemeral=ephemeral,
        )
    ]
    parts.extend(Message(content=text, embed=None, ephemeral=ephemeral) for text in continuation)
    return RenderedReply(parts=tuple(parts), feedback=response.conversation_id)


def attribution(asker: str) -> str:
    """The public answer's attribution line (Req 2.5). Mentions are suppressed by the sender."""
    return f"{asker} asked:"


def source_fields(sources: list[Source]) -> tuple[FieldSpec, ...]:
    """`title` + `snippet` only: a `doc_id` never reaches Discord (Req 2.1, 2.6)."""
    return tuple(
        FieldSpec(
            name=(source.title or SOURCE_FALLBACK)[:FIELD_NAME_LIMIT],
            value=_truncate(source.snippet, FIELD_VALUE_LIMIT),
        )
        for source in sources[:MAX_SOURCE_FIELDS]
    )


def usage_footer(usage: Usage, grounded: bool, *, part: int = 1, total: int = 1) -> str:
    """Grounding, cost and the position in the answer (Req 2.1)."""
    label = "grounded" if grounded else "not grounded"
    footer = (
        f"{label} · {usage.llm_calls} LLM call(s) · "
        f"{usage.prompt_tokens}/{usage.completion_tokens} tokens"
    )
    if total > 1:
        return f"part {part} of {total} · {footer}"
    return footer


def document_line(doc: DocInfo) -> str:
    """One document per block, id included: `doc_id` is the only handle `/forget` accepts."""
    summary = [f"**{doc.title or doc.doc_id}**", doc.visibility, doc.status]
    summary.append(f"{doc.chunk_count} chunk(s)")
    if doc.topic:
        summary.append(f"topic: {doc.topic}")
    if doc.expires_at:
        summary.append(f"expires: {doc.expires_at}")
    return f"{' · '.join(summary)}\n`{doc.doc_id}`"


def render_documents(docs: list[DocInfo], *, language: NoticeLanguage = "en") -> RenderedReply:
    """The corpus listing, split across parts so no document is dropped (Req 12.1-12.3)."""
    if not docs:
        return notice_reply(notice(NO_DOCUMENTS, language))
    blocks = [document_line(doc) for doc in docs]
    pages: list[tuple[list[str], int]] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        if current and length + len(block) + 2 > EMBED_DESCRIPTION_LIMIT:
            pages.append((current, length))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        pages.append((current, length))
    seen = 0
    parts: list[Message] = []
    for page, _ in pages:
        first = seen + 1
        seen += len(page)
        footer = f"showing {first}-{seen} of {len(docs)}"
        parts.append(
            Message(
                content="",
                embed=EmbedSpec(description="\n\n".join(page), footer=footer, colour=LIST_COLOUR),
                ephemeral=True,
            )
        )
    return RenderedReply(parts=tuple(parts))


def render_ingest(
    *, name: str, visibility: str, doc_id: str, language: NoticeLanguage = "en"
) -> RenderedReply:
    """What an ingest did. The API says `{doc_id}` only, so neither does this (Req 13.4)."""
    return notice_reply(
        notice(INGEST_DONE, language, name=name, visibility=visibility, doc_id=doc_id)
    )


def ingest_failure_reply(
    name: str, reason: str, *, language: NoticeLanguage = "en"
) -> RenderedReply:
    """A refused call or a `failed` ingest: never rendered as success (Req 13.4, 13.5)."""
    return notice_reply(notice(INGEST_FAILED, language, name=name, reason=reason))


def delete_result_reply(
    doc_id: str, deleted: bool, *, language: NoticeLanguage = "en"
) -> RenderedReply:
    """Req 14.3: `{\"ok\": false}` is a miss and must not read as a successful delete."""
    table = DELETE_DONE if deleted else DELETE_NOT_FOUND
    return notice_reply(notice(table, language, doc_id=doc_id))


def split_text(text: str, limit: int = MESSAGE_CONTENT_LIMIT) -> list[str]:
    """Split at paragraph, then line, then word boundaries; no character is dropped."""
    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = _break_point(remaining, limit)
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining or not parts:
        parts.append(remaining)
    return parts


def _mark_continuations(parts: list[str]) -> list[str]:
    """`…` on every part that is followed by another, so the split is visible (Req 2.4)."""
    if len(parts) < 2:
        return parts
    return [*(f"{part}{CONTINUATION_MARK}" for part in parts[:-1]), parts[-1]]


def _take(text: str, limit: int) -> tuple[str, str]:
    """Split once at ``limit``; returns the head and the untouched tail."""
    if len(text) <= limit:
        return text, ""
    cut = _break_point(text, limit)
    return text[:cut].rstrip(), text[cut:].lstrip()


def _break_point(text: str, limit: int) -> int:
    window = text[:limit]
    for separator in ("\n\n", "\n", " "):
        index = window.rfind(separator)
        if index > 0:
            return index + len(separator)
    return limit


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}{CONTINUATION_MARK}"
