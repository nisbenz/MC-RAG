"""The only place an HTTP call, a URL or a token lives (Req 7.1).

Nothing above this module sees an `httpx` exception: every failure becomes an `ApiFailure` with a
stable code, which is what the command layer maps to a notice. The token and the question text are
never logged or echoed.
"""

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from mcclub_rag.schemas import (
    ChatResponse,
    DocInfo,
    DocumentCreateResponse,
    HealthStatus,
    Visibility,
)

CHAT_PATH = "/api/v1/chat"
FEEDBACK_PATH = "/api/v1/feedback"
HEALTH_PATH = "/api/v1/health"
DOCUMENTS_PATH = "/api/v1/documents"
DOCUMENTS_URL_PATH = "/api/v1/documents/url"

FeedbackRating = Literal["up", "down"]

_STATUS_CODES: dict[int, str] = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    422: "validation_error",
    429: "rate_limited",
    503: "index_unavailable",
}


@dataclass(frozen=True)
class ApiFailure:
    """A call that did not produce a usable body."""

    code: str
    status: int | None = None


@dataclass(frozen=True)
class ChatOutcome:
    response: ChatResponse | None = None
    failure: ApiFailure | None = None


@dataclass(frozen=True)
class HealthOutcome:
    status: HealthStatus | None = None
    documents: int | None = None
    failure: ApiFailure | None = None


@dataclass(frozen=True)
class AdminOutcome:
    """An ingest or a delete: either a result or a failure, never both, never an exception."""

    doc_id: str | None = None
    deleted: bool | None = None
    failure: ApiFailure | None = None


@dataclass(frozen=True)
class DocumentsOutcome:
    documents: list[DocInfo] | None = None
    failure: ApiFailure | None = None


class RagClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float,
        member_token: str | None = None,
        admin_api_key: str | None = None,
        admin_timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._member_token = member_token
        self._admin_api_key = admin_api_key
        # An ingest answers only after parsing, OCR and embedding, so it gets its own budget: the
        # chat timeout would abandon every real document while the API went on to index it.
        self._admin_timeout = admin_timeout_s or timeout_s
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(
        self,
        question: str,
        *,
        as_member: bool,
        conversation_id: str | None = None,
    ) -> ChatOutcome:
        """One chat request. `conversation_id` is omitted entirely on a first turn (Req 4.1)."""
        body: dict[str, object] = {"message": question}
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        try:
            response = await self._client.post(CHAT_PATH, json=body, headers=self._auth(as_member))
        except httpx.TimeoutException:
            return ChatOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return ChatOutcome(failure=ApiFailure("unreachable"))
        if response.status_code != httpx.codes.OK:
            return ChatOutcome(failure=self._failure(response))
        try:
            return ChatOutcome(response=ChatResponse.model_validate(response.json()))
        except (ValueError, ValidationError):
            return ChatOutcome(failure=ApiFailure("internal", response.status_code))

    async def feedback(self, conversation_id: str, rating: FeedbackRating) -> bool:
        """True when the rating was stored. A failure here never reaches the member's answer."""
        try:
            response = await self._client.post(
                FEEDBACK_PATH, json={"conversation_id": conversation_id, "rating": rating}
            )
            return response.status_code == httpx.codes.OK and bool(response.json().get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def health(self) -> HealthOutcome:
        try:
            response = await self._client.get(HEALTH_PATH)
        except httpx.TimeoutException:
            return HealthOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return HealthOutcome(failure=ApiFailure("unreachable"))
        if response.status_code != httpx.codes.OK:
            return HealthOutcome(failure=self._failure(response))
        try:
            payload = response.json()
            return HealthOutcome(status=payload["status"], documents=payload["documents"])
        except (ValueError, KeyError, TypeError):
            return HealthOutcome(failure=ApiFailure("internal", response.status_code))

    async def ingest_file(
        self,
        filename: str,
        data: bytes,
        *,
        visibility: Visibility,
        topic: str | None = None,
    ) -> AdminOutcome:
        """`POST /api/v1/documents` with the bytes as the multipart `file` part (Req 13.1)."""
        try:
            response = await self._client.post(
                DOCUMENTS_PATH,
                data=_document_form(visibility, topic),
                files={"file": (filename, data)},
                headers=self._admin_auth(),
                timeout=self._admin_timeout,
            )
        except httpx.TimeoutException:
            return AdminOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return AdminOutcome(failure=ApiFailure("unreachable"))
        return _created(response)

    async def ingest_url(
        self,
        url: str,
        *,
        visibility: Visibility,
        topic: str | None = None,
    ) -> AdminOutcome:
        """`POST /api/v1/documents/url` (Req 15.1). The URL is fetched by the API, never here."""
        form = _document_form(visibility, topic)
        form["url"] = url
        try:
            response = await self._client.post(
                DOCUMENTS_URL_PATH,
                data=form,
                headers=self._admin_auth(),
                timeout=self._admin_timeout,
            )
        except httpx.TimeoutException:
            return AdminOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return AdminOutcome(failure=ApiFailure("unreachable"))
        return _created(response)

    async def list_documents(self) -> DocumentsOutcome:
        """`GET /api/v1/documents` (Req 12.1)."""
        try:
            response = await self._client.get(DOCUMENTS_PATH, headers=self._admin_auth())
        except httpx.TimeoutException:
            return DocumentsOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return DocumentsOutcome(failure=ApiFailure("unreachable"))
        if response.status_code != httpx.codes.OK:
            return DocumentsOutcome(failure=self._failure(response))
        try:
            return DocumentsOutcome(
                documents=[DocInfo.model_validate(item) for item in response.json()]
            )
        except (ValueError, ValidationError, TypeError):
            return DocumentsOutcome(failure=ApiFailure("internal", response.status_code))

    async def delete_document(self, doc_id: str) -> AdminOutcome:
        """`DELETE /api/v1/documents/{doc_id}`; `{"ok": false}` is a miss (Req 14.3)."""
        try:
            response = await self._client.delete(
                f"{DOCUMENTS_PATH}/{doc_id}",
                headers=self._admin_auth(),
            )
        except httpx.TimeoutException:
            return AdminOutcome(failure=ApiFailure("timeout"))
        except httpx.HTTPError:
            return AdminOutcome(failure=ApiFailure("unreachable"))
        if response.status_code != httpx.codes.OK:
            return AdminOutcome(failure=self._failure(response))
        try:
            return AdminOutcome(deleted=bool(response.json()["ok"]))
        except (ValueError, KeyError, TypeError):
            return AdminOutcome(failure=ApiFailure("internal", response.status_code))

    def _auth(self, as_member: bool) -> dict[str, str]:
        if as_member and self._member_token:
            return {"Authorization": f"Bearer {self._member_token}"}
        return {}

    def _admin_auth(self) -> dict[str, str]:
        """Req 11.4: the admin key rides admin calls only, never a chat or feedback request."""
        if self._admin_api_key:
            return {"X-Admin-Key": self._admin_api_key}
        return {}

    @staticmethod
    def _failure(response: httpx.Response) -> ApiFailure:
        fallback = _STATUS_CODES.get(response.status_code, "internal")
        try:
            code = response.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return ApiFailure(fallback, response.status_code)
        return ApiFailure(code if isinstance(code, str) else fallback, response.status_code)


def _document_form(visibility: Visibility, topic: str | None) -> dict[str, str]:
    """The form fields both ingest routes share. An absent topic is left out, not sent empty."""
    form = {"visibility": visibility}
    if topic:
        form["topic"] = topic
    return form


def _created(response: httpx.Response) -> AdminOutcome:
    if response.status_code != httpx.codes.OK:
        return AdminOutcome(failure=RagClient._failure(response))
    try:
        return AdminOutcome(doc_id=DocumentCreateResponse.model_validate(response.json()).doc_id)
    except (ValueError, ValidationError):
        return AdminOutcome(failure=ApiFailure("internal", response.status_code))
