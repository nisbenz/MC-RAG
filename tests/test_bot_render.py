"""What a member sees: grounding, sources, splitting, language (Req 2)."""

from mcclub_rag.discordbot.render import (
    CONTINUATION_MARK,
    EMBED_DESCRIPTION_LIMIT,
    GROUNDED_COLOUR,
    MESSAGE_CONTENT_LIMIT,
    UNGROUNDED_COLOUR,
    RenderedReply,
    notice_reply,
    render_answer,
    split_text,
)
from mcclub_rag.schemas import Source
from tests.bot_fakes import ANSWER_TEXT, chat_response

ARABIC_ANSWER = "يجتمع النادي كل ثلاثاء في الورشة، والدخول مجاني للأعضاء."
LONG_ANSWER = "\n\n".join(f"فقرة {index} " + "x" * 300 for index in range(20))


def _texts(reply: RenderedReply) -> list[str]:
    return [
        reply.parts[0].embed.description,
        *(part.content for part in reply.parts[1:]),
    ]


def _flat(reply: RenderedReply) -> str:
    """Every string the reply could leak, for the doc_id assertion."""
    pieces = [part.content for part in reply.parts]
    for part in reply.parts:
        if part.embed is not None:
            pieces.append(part.embed.description)
            pieces.append(part.embed.footer or "")
            pieces.extend(f"{field.name} {field.value}" for field in part.embed.fields)
    return "\n".join(pieces)


def _normalised(text: str) -> str:
    return text.replace(CONTINUATION_MARK, "").replace(" ", "").replace("\n", "")


def test_short_answer_is_one_part() -> None:
    reply = render_answer(chat_response(), public=False, asker="Sam", language="en")
    assert len(reply.parts) == 1
    part = reply.parts[0]
    assert part.embed is not None
    assert part.embed.description == ANSWER_TEXT
    assert part.embed.colour == GROUNDED_COLOUR
    assert part.ephemeral is True
    assert part.content == ""
    assert reply.feedback == "c_test"


def test_sources_show_titles_and_never_a_doc_id() -> None:
    reply = render_answer(chat_response(), public=False, asker="Sam", language="en")
    fields = reply.parts[0].embed.fields
    assert [field.name for field in fields] == ["Club rules"]
    assert fields[0].value == "We meet on Tuesdays."
    assert "doc-1" not in _flat(reply)


def test_source_without_a_title_still_renders_without_its_doc_id() -> None:
    sources = [Source(doc_id="doc-9", title="", snippet="Snippet only.")]
    reply = render_answer(chat_response(sources=sources), public=False, asker="Sam", language="en")
    assert reply.parts[0].embed.fields[0].name == "Source"
    assert "doc-9" not in _flat(reply)


def test_ungrounded_answer_is_labelled() -> None:
    reply = render_answer(
        chat_response(answer="I don't know.", grounded=False, sources=[]),
        public=False,
        asker="Sam",
        language="en",
    )
    embed = reply.parts[0].embed
    assert embed.description.startswith("Not grounded in the documents.")
    assert embed.colour == UNGROUNDED_COLOUR
    assert embed.fields == ()
    assert "not grounded" in embed.footer


def test_ungrounded_label_follows_the_question_language() -> None:
    reply = render_answer(
        chat_response(answer="لا أعرف.", grounded=False, sources=[]),
        public=False,
        asker="Sam",
        language="ar",
    )
    assert reply.parts[0].embed.description.startswith("غير مستند إلى الوثائق.")


def test_public_answer_attributes_the_asker() -> None:
    reply = render_answer(chat_response(), public=True, asker="Sam", language="en")
    assert reply.parts[0].content == "Sam asked:"
    assert all(part.ephemeral is False for part in reply.parts)


def test_long_answer_splits_in_order_without_losing_text() -> None:
    reply = render_answer(
        chat_response(answer=LONG_ANSWER), public=False, asker="Sam", language="en"
    )
    assert len(reply.parts) > 1
    joined = _normalised("\n".join(_texts(reply)))
    assert joined == _normalised(LONG_ANSWER)


def test_every_part_that_continues_is_marked() -> None:
    reply = render_answer(
        chat_response(answer=LONG_ANSWER), public=False, asker="Sam", language="en"
    )
    texts = _texts(reply)
    assert len(texts) > 1
    for text in texts[:-1]:
        assert text.endswith(CONTINUATION_MARK)
    assert not texts[-1].endswith(CONTINUATION_MARK)


def test_split_respects_both_limits() -> None:
    reply = render_answer(
        chat_response(answer=LONG_ANSWER), public=False, asker="Sam", language="en"
    )
    assert len(reply.parts[0].embed.description) <= EMBED_DESCRIPTION_LIMIT + 1
    for part in reply.parts[1:]:
        assert len(part.content) <= MESSAGE_CONTENT_LIMIT + 1


def test_usage_and_grounding_are_in_the_footer() -> None:
    reply = render_answer(
        chat_response(answer=LONG_ANSWER), public=False, asker="Sam", language="en"
    )
    footer = reply.parts[0].embed.footer
    assert "grounded" in footer
    assert "2 LLM call(s)" in footer
    assert "100/20 tokens" in footer
    assert footer.startswith("part 1 of ")


def test_rtl_answer_is_passed_through_unchanged() -> None:
    reply = render_answer(
        chat_response(answer=ARABIC_ANSWER), public=False, asker="Sam", language="ar"
    )
    assert reply.parts[0].embed.description == ARABIC_ANSWER


def test_split_text_never_drops_characters() -> None:
    text = "word " * 900
    parts = split_text(text)
    assert len(parts) > 1
    assert "".join(parts).replace(" ", "") == text.replace(" ", "")


def test_split_text_hard_cuts_a_word_longer_than_the_limit() -> None:
    parts = split_text("y" * 4000)
    assert all(len(part) <= MESSAGE_CONTENT_LIMIT for part in parts)
    assert sum(len(part) for part in parts) == 4000
    assert "".join(parts) == "y" * 4000


def test_split_text_leaves_a_short_text_alone() -> None:
    assert split_text("short") == ["short"]
    assert split_text("") == [""]


def test_notice_reply_has_no_embed_and_is_ephemeral() -> None:
    reply = notice_reply("Something went wrong.")
    assert len(reply.parts) == 1
    assert reply.parts[0].content == "Something went wrong."
    assert reply.parts[0].embed is None
    assert reply.parts[0].ephemeral is True
    assert reply.feedback is None
