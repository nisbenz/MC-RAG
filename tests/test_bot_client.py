"""The API client: headers, body shape, and every failure code (Req 3.1, 6.2, 7.1).

The real error envelope from `errors.py` is used verbatim, so an API-side change shows up here.
"""

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from mcclub_rag.discordbot.client import RagClient

ANSWER = "The club meets on Tuesdays."
CHAT_PAYLOAD: dict[str, Any] = {
    "answer": ANSWER,
    "conversation_id": "c_abc",
    "grounded": True,
    "sources": [{"doc_id": "doc-1", "title": "Club rules", "snippet": "We meet on Tuesdays."}],
    "usage": {"llm_calls": 2, "prompt_tokens": 100, "completion_tokens": 20},
    "latency_ms": 1200,
}
MEMBER_TOKEN = "member-secret"


def _client(handler: Any, *, timeout_s: float = 5.0) -> RagClient:
    return RagClient(
        "http://api:8000",
        timeout_s=timeout_s,
        member_token=MEMBER_TOKEN,
        transport=httpx.MockTransport(handler),
    )


ADMIN_KEY = "admin-secret"
DOC_ID = "d0c-1"
DOC_INFO: dict[str, Any] = {
    "doc_id": DOC_ID,
    "title": "Statuts 2026",
    "visibility": "members",
    "status": "indexed",
    "chunk_count": 12,
    "created_at": "2026-10-16T12:00:00Z",
    "topic": "statuts",
    "expires_at": None,
}


def _admin_client(
    handler: Any, *, key: str | None = ADMIN_KEY, member_token: str | None = None
) -> RagClient:
    return RagClient(
        "http://api:8000",
        timeout_s=5.0,
        admin_timeout_s=900.0,
        admin_api_key=key,
        member_token=member_token,
        transport=httpx.MockTransport(handler),
    )


async def test_an_ingest_posts_the_file_as_multipart_with_the_admin_key() -> None:
    """Req 13.1: the attachment is the `file` part, and the fields match the API's form."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-admin-key")
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"doc_id": DOC_ID})

    client = _admin_client(handler)
    outcome = await client.ingest_file(
        "statuts.pdf", b"%PDF-1.7 fake", visibility="members", topic="statuts"
    )
    await client.aclose()

    assert outcome.failure is None
    assert outcome.doc_id == DOC_ID
    assert seen["url"] == "http://api:8000/api/v1/documents"
    assert seen["key"] == ADMIN_KEY
    assert seen["content_type"].startswith("multipart/form-data")
    assert 'filename="statuts.pdf"' in seen["body"]
    assert 'name="visibility"' in seen["body"]
    assert "members" in seen["body"]
    assert 'name="topic"' in seen["body"]


async def test_the_ingest_budget_is_not_the_chat_budget() -> None:
    """A 60-second chat timeout would abandon every real scan the API then goes on to index."""
    admin = _admin_client(lambda request: httpx.Response(200, json={"doc_id": DOC_ID}))
    chat = _client(lambda request: httpx.Response(200, json=CHAT_PAYLOAD))
    assert admin._admin_timeout == 900.0  # noqa: SLF001 - the value itself is the contract
    assert chat._admin_timeout == 5.0  # noqa: SLF001 - unset falls back to the chat budget
    await admin.aclose()
    await chat.aclose()


async def test_an_ingest_leaves_an_absent_topic_out_of_the_form() -> None:
    """Sending `topic=` empty would tell the API the topic is an empty string, not "derive it"."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"doc_id": DOC_ID})

    client = _admin_client(handler)
    await client.ingest_file("notes.txt", b"hello", visibility="public")
    await client.aclose()

    assert "topic" not in seen["body"]
    assert "visibility" in seen["body"]


async def test_a_url_ingest_posts_the_url_as_a_field_and_no_file() -> None:
    """Req 15.1: the API fetches the URL, so this call never touches the network itself."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["path"] = request.url.path
        seen["body"] = request.content.decode("utf-8", "replace")
        seen["key"] = request.headers.get("x-admin-key")
        return httpx.Response(200, json={"doc_id": DOC_ID})

    client = _admin_client(handler)
    outcome = await client.ingest_url(
        "https://example.org/rules", visibility="public", topic="rules"
    )
    await client.aclose()

    assert outcome.doc_id == DOC_ID
    assert seen["path"] == "/api/v1/documents/url"
    # The URL is form-encoded, so decode the body rather than matching it raw.
    fields = parse_qs(seen["body"])
    assert fields["url"] == ["https://example.org/rules"]
    assert fields["visibility"] == ["public"]
    assert fields["topic"] == ["rules"]
    assert "filename" not in seen["body"]  # no file part on this route
    assert seen["key"] == ADMIN_KEY


async def test_listing_documents_parses_the_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[DOC_INFO])

    client = _admin_client(handler)
    outcome = await client.list_documents()
    await client.aclose()

    assert outcome.failure is None
    assert outcome.documents is not None
    assert outcome.documents[0].title == "Statuts 2026"
    assert outcome.documents[0].visibility == "members"
    assert outcome.documents[0].chunk_count == 12


async def test_an_empty_corpus_is_an_empty_list_not_a_failure() -> None:
    client = _admin_client(lambda request: httpx.Response(200, json=[]))
    outcome = await client.list_documents()
    await client.aclose()
    assert outcome.failure is None
    assert outcome.documents == []


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (422, "validation_error"),
        (429, "rate_limited"),
        (503, "index_unavailable"),
        (500, "internal"),
    ],
)
async def test_an_admin_failure_carries_the_envelope_code(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})

    client = _admin_client(handler)
    outcome = await client.list_documents()
    await client.aclose()

    assert outcome.documents is None
    assert outcome.failure is not None
    assert outcome.failure.code == code
    assert outcome.failure.status == status


async def test_an_admin_call_that_cannot_connect_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _admin_client(handler)
    outcome = await client.ingest_url("https://example.org", visibility="public")
    await client.aclose()

    assert outcome.failure is not None
    assert outcome.failure.code == "unreachable"


async def test_a_malformed_admin_body_is_internal_not_a_traceback() -> None:
    """A 200 whose body does not match the schema must not raise inside a Discord handler."""

    async def scenario() -> None:
        client = _admin_client(lambda request: httpx.Response(200, json={"nope": True}))
        outcome = await client.ingest_file("a.txt", b"x", visibility="public")
        await client.aclose()
        assert outcome.doc_id is None
        assert outcome.failure is not None
        assert outcome.failure.code == "internal"

    await scenario()


async def test_deleting_distinguishes_a_miss_from_a_success() -> None:
    """Req 14.3: `{"ok": false}` is a 200, and it must not read as "deleted"."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.path)
        return httpx.Response(200, json={"ok": request.url.path.endswith(DOC_ID)})

    client = _admin_client(handler)
    hit = await client.delete_document(DOC_ID)
    miss = await client.delete_document("d0c-404")
    await client.aclose()

    assert hit.failure is None
    assert hit.deleted is True
    assert miss.failure is None
    assert miss.deleted is False
    assert sent == [f"/api/v1/documents/{DOC_ID}", "/api/v1/documents/d0c-404"]


async def test_the_admin_key_never_rides_a_chat_or_feedback_request() -> None:
    """Req 11.4: the key is for the document routes only."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if request.url.path == "/api/v1/chat":
            return httpx.Response(200, json=CHAT_PAYLOAD)
        return httpx.Response(200, json={"ok": True})

    client = _admin_client(handler, member_token=MEMBER_TOKEN)
    await client.ask("Where is the club?", as_member=True)
    await client.feedback("c_abc", "up")
    await client.health()
    await client.aclose()

    assert len(seen) == 3
    for headers in seen:
        assert "x-admin-key" not in headers
    # The member header still rides the chat call: a configured admin key changes nothing there.
    assert seen[0]["authorization"] == f"Bearer {MEMBER_TOKEN}"


async def test_a_client_without_an_admin_key_sends_no_admin_header() -> None:
    """Unconfigured must mean refused by the API, never "allowed because we sent nothing"."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-admin-key")
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}})

    client = _admin_client(handler, key=None)
    outcome = await client.ingest_url("https://example.org", visibility="public")
    await client.aclose()

    assert seen["key"] is None
    assert outcome.failure is not None
    assert outcome.failure.code == "unauthorized"


async def test_member_ask_sends_the_bearer_and_omits_the_conversation_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_PAYLOAD)

    client = _client(handler)
    outcome = await client.ask("Where is the club?", as_member=True)
    await client.aclose()

    assert outcome.failure is None
    assert outcome.response is not None
    assert outcome.response.answer == ANSWER
    assert outcome.response.usage.llm_calls == 2
    assert seen["auth"] == f"Bearer {MEMBER_TOKEN}"
    assert seen["url"] == "http://api:8000/api/v1/chat"
    assert seen["body"] == {"message": "Where is the club?"}


async def test_public_ask_sends_no_header_and_echoes_the_id() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=CHAT_PAYLOAD)

    client = _client(handler)
    await client.ask("Where is the club?", as_member=False, conversation_id="c_abc")
    await client.aclose()

    assert seen["auth"] is None
    assert seen["body"] == {"message": "Where is the club?", "conversation_id": "c_abc"}


async def test_a_member_without_a_configured_token_sends_no_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=CHAT_PAYLOAD)

    client = RagClient("http://api:8000", timeout_s=5.0, transport=httpx.MockTransport(handler))
    await client.ask("hi", as_member=True)
    await client.aclose()

    assert seen["auth"] is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (422, "validation_error"),
        (429, "rate_limited"),
        (503, "index_unavailable"),
    ],
)
async def test_api_errors_keep_their_envelope_code(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})

    client = _client(handler)
    outcome = await client.ask("hi", as_member=False)
    await client.aclose()

    assert outcome.response is None
    assert outcome.failure is not None
    assert outcome.failure.code == code
    assert outcome.failure.status == status


async def test_a_bodyless_error_falls_back_to_its_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>boom</html>")

    client = _client(handler)
    outcome = await client.ask("hi", as_member=False)
    await client.aclose()

    assert outcome.failure is not None
    assert outcome.failure.code == "internal"
    assert outcome.failure.status == 500


async def test_a_two_hundred_with_a_broken_body_is_internal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = _client(handler)
    outcome = await client.ask("hi", as_member=False)
    await client.aclose()

    assert outcome.failure is not None
    assert outcome.failure.code == "internal"


@pytest.mark.parametrize(
    ("error", "code"),
    [(httpx.ConnectError("boom"), "unreachable"), (httpx.ReadTimeout("slow"), "timeout")],
)
async def test_transport_failures_become_codes(error: Exception, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    client = _client(handler)
    outcome = await client.ask("hi", as_member=False)
    await client.aclose()

    assert outcome.failure is not None
    assert outcome.failure.code == code


async def test_feedback_posts_the_rating() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    stored = await client.feedback("c_abc", "up")
    await client.aclose()

    assert stored is True
    assert seen["url"] == "http://api:8000/api/v1/feedback"
    assert seen["body"] == {"conversation_id": "c_abc", "rating": "up"}


@pytest.mark.parametrize("status", [400, 500])
async def test_feedback_failure_is_false_not_an_exception(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": "internal", "message": "nope"}})

    client = _client(handler)
    assert await client.feedback("c_abc", "down") is False
    await client.aclose()


async def test_feedback_ok_false_is_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    client = _client(handler)
    assert await client.feedback("c_abc", "up") is False
    await client.aclose()


async def test_feedback_survives_an_unreachable_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    assert await client.feedback("c_abc", "up") is False
    await client.aclose()


async def test_health_reads_status_and_documents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "documents": 60})

    client = _client(handler)
    outcome = await client.health()
    await client.aclose()

    assert outcome.failure is None
    assert outcome.status == "ok"
    assert outcome.documents == 60


async def test_health_failure_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    outcome = await client.health()
    await client.aclose()

    assert outcome.status is None
    assert outcome.failure is not None
    assert outcome.failure.code == "unreachable"
