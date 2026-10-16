# Requirements Document

## Introduction

A Discord front end for the existing chat API. The bot is a **thin client**: it speaks
`POST /api/v1/chat` over HTTP and renders the answer, its sources and the grounding state as a
Discord reply. It adds no retrieval, no generation and no new API surface.

Decided with the user on 2026-10-16 (branch `discord-exp`): the API already returns everything a
chat front end needs (`answer`, `grounded`, `sources[{title, snippet}]`, `usage`, `conversation_id`),
and it stays frozen, so the bot consumes the contract and adds no change to any existing module in
`src/mcclub_rag/`: the bot is a new, self-contained subpackage that no app module imports. The bot is a **separate process and container**, so the 4 GB / 2-core VPS
budget, the single-worker app and the CI job list stay as they are.

**Non-goals:** no document upload from Discord (admins keep using `POST /api/v1/documents`), no
voice, no threads-per-question history beyond the API's 3 turns, no bot-to-bot or webhook ingress.

## Requirements

### Requirement 1: Asking a question

**User Story:** As a club member, I want to ask the RAG chatbot a question from Discord, so that I
get an answer without leaving the server.

#### Acceptance Criteria

1. The bot SHALL expose exactly three slash commands — `/ask` (a required `question` string option
   and an optional `public` boolean), `/reset` and `/status` — and only `/ask` SHALL reach the chat
   API. WHEN `DISCORD_GUILD_ID` is set THEN the commands SHALL be registered as guild commands, and
   otherwise SHALL be registered globally.
2. The bot SHALL support slash commands only: it SHALL NOT request the Message Content privileged
   intent and SHALL NOT read arbitrary channel messages, so it needs no privileged intent and no
   Discord verification (verification is required past 100 guilds).
3. WHEN `/ask` is received THEN the bot SHALL acknowledge the interaction with `defer()` **before**
   calling the API, because Discord requires a first response within 3 s and p95 latency of the chat
   route is above that.
4. WHEN `question` is longer than 2000 characters THEN the bot SHALL refuse the interaction with a
   short localized notice; the bot SHALL NOT truncate a question silently and SHALL NOT send an
   over-long body that the API would answer with `422 validation_error`.
5. WHEN `question` is blank or whitespace-only THEN the bot SHALL forward it as typed (the API
   answers a blank message with a prompt, `grounded: false`); the bot SHALL NOT pre-empt that
   behaviour.
6. WHEN questions arrive concurrently in different channels THEN they SHALL be independent: each
   interaction SHALL get its own request, its own API call and its own edit of its own response.

### Requirement 2: Rendering the answer

**User Story:** As a member, I want the answer, its sources and whether it is grounded, so that I
can judge how much to trust what I read.

#### Acceptance Criteria

1. The bot SHALL show the `answer` text, the `grounded` state, the sources (`title` + `snippet`,
   never `doc_id`) and `usage` (llm_calls, prompt_tokens, completion_tokens).
2. WHEN `grounded` is false (a refusal or a degraded reply) THEN the reply SHALL label it as not
   grounded, so a member can tell an honest "not in the corpus" from an answer.
3. Arabic, darija, arabizi, French and English SHALL render as sent. Text direction SHALL follow the
   answer's own script; there SHALL be no transliteration and no rewriting.
4. WHEN an answer exceeds the Discord message limit THEN the bot SHALL split it across follow-up
   messages, in order, without dropping text silently, with a `…` marker on every part that is
   followed by another, so the continuation is visible at the split point.
5. Every message the bot sends SHALL be `ephemeral` **unless** the user asks for a public answer
   (`/ask public: true`), so a club channel is not flooded by one person's question. WHEN the answer
   is public THEN the bot SHALL post it with an attribution line naming the asker.
6. The bot SHALL NOT render retrieved passage text the answer did not use: only the API's `answer`
   field and its `sources` SHALL reach Discord.

### Requirement 3: Roles and access

**User Story:** As a member holding the club role, I want members-only documents to be visible to me
and hidden from everyone else, so that the corpus and the API's access rules stay authoritative.

#### Acceptance Criteria

1. WHEN the interaction is in `DISCORD_GUILD_ID` AND the asker's Discord account holds the configured
   member role THEN the bot SHALL send `Authorization: Bearer <MEMBER_TOKEN>`. In every other guild,
   in a DM, and whenever either id is unset, it SHALL send no header, so the API answers as `public`
   and members-only documents stay invisible. The token SHALL never be part of a message, an embed or
   a log line.
2. The member role id SHALL be configurable (`DISCORD_MEMBER_ROLE_ID`); WHEN it is unset THEN every
   question SHALL be public, which is the demo default.
3. WHEN the API answers `401 unauthorized` THEN the bot SHALL answer with a short "ask the admins"
   notice, SHALL NOT retry and SHALL NOT echo the token.

### Requirement 4: Conversations across questions

**User Story:** As a member, I want a follow-up question in the same channel to build on the
previous one, so that short questions still make sense to retrieval.

#### Acceptance Criteria

1. The bot SHALL omit `conversation_id` on the first question in a channel and thereafter SHALL send
   back the `conversation_id` the API returned for that channel, because the API mints a fresh id for
   any id it does not recognise; a follow-up in the same channel SHALL therefore reach the API with
   the previous turns and retrieval can merge them.
2. The bot SHALL tolerate the API forgetting the conversation (in-memory store, 30 min TTL, 3
   turns): an answer that comes back as a first-turn answer SHALL be correct behaviour, not an error,
   and SHALL NOT be reported to the user as one.
3. The bot's local cache key SHALL be namespaced (`mc-<guild>-<channel>`), so two guilds cannot
   collide on one entry, and the bot SHALL NOT send a key of its own invention as `conversation_id`.
4. WHEN `/reset` is used THEN the bot SHALL start a new conversation in the current channel by
   forgetting its cached id, so the next `/ask` omits `conversation_id`; the old conversation SHALL be
   left to expire on its own.

### Requirement 5: Feedback

**User Story:** As a member, I want to rate an answer 👍 or 👎, so that the club can see which
answers were useful.

#### Acceptance Criteria

1. Each answer SHALL carry a 👍 / 👎 pair of buttons; WHEN one is pressed THEN the bot SHALL call
   `POST /api/v1/feedback` with the answer's `conversation_id` and `rating` `up` / `down`.
2. WHEN a rating is pressed THEN the pressed button SHALL be disabled and the other removed, so a
   rating cannot be changed or counted twice, and the press SHALL be confirmed with an ephemeral
   note.
3. WHEN the feedback call fails THEN it SHALL never disturb the answer already delivered; it SHALL be
   logged, not shown.

### Requirement 6: Staying inside the budget

**User Story:** As a club admin, I want the bot to respect the LLM budget and the API's rate limit,
so that one member cannot spend the whole day's quota for everyone.

#### Acceptance Criteria

1. The bot SHALL enforce a per-user cooldown (`DISCORD_USER_COOLDOWN_S`, default 20 s) and a
   per-guild concurrency cap before it calls the API, so one member cannot spend the server's quota.
2. The API's per-IP sliding window (`CHAT_RATE_LIMIT_REQUESTS` / `_WINDOW_S`, 20 per 60 s by default)
   SHALL apply to the bot as one client IP. WHEN the API answers `429 rate_limited` THEN the bot SHALL
   tell the user to retry shortly and SHALL NOT retry automatically, so it never turns into a retry
   storm. The API sends no `Retry-After` and no remaining-seconds field, so the bot SHALL NOT state a
   precise wait.
3. The bot SHALL NOT call the API in a loop, SHALL NOT retry a chat call on its own, and SHALL NOT
   issue a second call for the same question: at most one `/ask` = at most 1 chat request = at most
   2 LLM calls (the rewrite plus the answer).
4. The daily LLM budget (`LLM_DAILY_LIMIT`, 50 by default) SHALL be treated as a shared club-wide
   resource. The bot MAY report API health (`GET /api/v1/health`), the document count and its own
   cooldown and concurrency settings in a `/status` command; because `/health` exposes no LLM
   headroom, the bot SHALL NOT claim one, and it SHALL NOT raise or bypass any limit.

### Requirement 7: Failure behaviour

**User Story:** As a member, I want a clear error when something breaks, so that I know whether to
retry, and never see a crash or a raw response body.

#### Acceptance Criteria

1. WHEN the API is unreachable, timing out or answering `5xx` THEN the bot SHALL produce a short
   ephemeral notice naming the problem, SHALL NOT show a Discord traceback and SHALL NOT show a raw
   JSON body.
2. WHEN the API answers `503 index_unavailable` or `429 rate_limited` THEN the error SHALL be
   reported in the language of the question, using the wording pattern the API already uses for its
   own notices.
3. IF the interaction has expired before the answer arrives (Discord's interaction token lasts 15
   minutes) THEN the bot SHALL NOT publish an ephemeral answer into the channel: for a `public: true`
   question it SHALL post the answer as a follow-up message, and otherwise it SHALL post a short
   notice asking the member to ask again and drop the answer. It SHALL log which of the two it did.
4. WHEN a Discord API call fails (rate limit, missing permission, unknown channel) THEN it SHALL be
   caught and logged; a Discord failure SHALL never crash the process.
5. The bot SHALL log through structlog with the same shape as the app: one event per interaction with
   ids, latency and outcome, and SHALL never log the question text, the answer text or the token.

### Requirement 8: Configuration and secrets

**User Story:** As an operator, I want the bot configured by environment variables like the rest of
the stack, so that it deploys the same way and no secret ends up in the repo.

#### Acceptance Criteria

1. Configuration SHALL be env-driven and documented in `.env.example` with a comment line each:
   `DISCORD_BOT_TOKEN`, `DISCORD_RAG_BASE_URL`, `DISCORD_GUILD_ID` (optional), `DISCORD_MEMBER_ROLE_ID`
   (optional), `DISCORD_USER_COOLDOWN_S`, `DISCORD_MAX_CONCURRENCY`, `DISCORD_API_TIMEOUT_S`.
2. The bot SHALL read the token from the environment only. It SHALL never be logged, sent in a
   message, written to a file or committed; `.env` SHALL stay ignored.
3. `DISCORD_RAG_BASE_URL` SHALL default to `http://app:8000`, so the bot reaches the app over the
   Compose network and no host port has to be published for it.

### Requirement 9: Running it

**User Story:** As an operator, I want to run the bot beside the existing stack with one command, so
that deploying it does not change how the app itself runs.

#### Acceptance Criteria

1. The bot SHALL ship as its own Dockerfile (or a Compose service reusing the base image) and SHALL
   start with one command next to the existing stack; `docker compose up` SHALL still bring up the
   current two services unchanged when the bot is not asked for.
2. The bot SHALL add no inbound port to the app and SHALL not require a public URL, TLS or a reverse
   proxy: it dials out to Discord's gateway.
3. The dependency SHALL be added per the repo rules: verified on PyPI, `~=`-pinned, `uv.lock`
   regenerated, and `uv lock --check` plus ruff and the fast pytest SHALL stay green.
4. The bot SHALL need no model file, so it runs without `MODELS_DIR`, tessdata or the embedder: the
   app owns those, the bot owns no model.

### Requirement 10: Tests

**User Story:** As a maintainer, I want the bot's behaviour covered without Discord or a running API,
so that the CI job stays fast and offline.

#### Acceptance Criteria

1. Every requirement above SHALL have automated coverage that runs without Discord and without the
   API (a fake client and `httpx.MockTransport`), in the fast pytest job with no model markers.
2. Coverage SHALL include at minimum: role-to-header mapping, `conversation_id` echoing and
   namespacing, cooldown and concurrency refusals, `429` and `5xx` and unreachable handling, answer
   splitting at the Discord limit, grounding label, feedback button wiring and one-shot disabling, and
   that no log record or message contains the token or the question text.
3. The bot SHALL not be scored by the e2e harness; `eval/e2e/` SHALL stay about the API.
