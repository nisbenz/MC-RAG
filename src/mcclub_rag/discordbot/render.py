"""Turning a `ChatResponse` into Discord-shaped parts (Req 2).

Everything a member sees is decided here, in plain dataclasses, so it is testable without a gateway.
The Discord types only appear in `app.py`, which sends what this module produced.
"""

from dataclasses import dataclass

from mcclub_rag.discordbot.notices import NOT_GROUNDED, NoticeLanguage, notice
from mcclub_rag.schemas import ChatResponse, Source, Usage

EMBED_DESCRIPTION_LIMIT = 4000  # Discord's embed description cap
MESSAGE_CONTENT_LIMIT = 1900  # under Discord's 2000 so a `…` marker still fits
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_SOURCE_FIELDS = 10  # Discord allows 25; ten is all a member reads
CONTINUATION_MARK = "\u2026"
SOURCE_FALLBACK = "Source"
GROUNDED_COLOUR = 0x2ECC71
UNGROUNDED_COLOUR = 0x95A5A6


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
