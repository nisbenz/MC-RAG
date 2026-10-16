"""The API client: headers, body shape, and every failure code (Req 3.1, 6.2, 7.1).

The real error envelope from `errors.py` is used verbatim, so an API-side change shows up here.
"""

import json
from typing import Any

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
