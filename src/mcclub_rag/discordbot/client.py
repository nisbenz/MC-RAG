"""The only place an HTTP call, a URL or a token lives (Req 7.1).

Nothing above this module sees an `httpx` exception: every failure becomes an `ApiFailure` with a
stable code, which is what the command layer maps to a notice. The token and the question text are
never logged or echoed.
"""

from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import ValidationError

from mcclub_rag.schemas import ChatResponse, HealthStatus

CHAT_PATH = "/api/v1/chat"
FEEDBACK_PATH = "/api/v1/feedback"
HEALTH_PATH = "/api/v1/health"

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


class RagClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float,
        member_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._member_token = member_token
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

    def _auth(self, as_member: bool) -> dict[str, str]:
        if as_member and self._member_token:
            return {"Authorization": f"Bearer {self._member_token}"}
        return {}

    @staticmethod
    def _failure(response: httpx.Response) -> ApiFailure:
        fallback = _STATUS_CODES.get(response.status_code, "internal")
        try:
            code = response.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return ApiFailure(fallback, response.status_code)
        return ApiFailure(code if isinstance(code, str) else fallback, response.status_code)
