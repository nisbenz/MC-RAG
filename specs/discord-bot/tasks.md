# Implementation Plan

Test-first throughout: each numbered step lands its tests in the same step, and every test runs in
the existing fast pytest job with no model markers. `specs/discord-bot/design.md` is the reference for
every interface named below.

- [x] 1. Add the dependency and the package skeleton
  - Add `discord.py~=2.7` to `[project] dependencies` and run `uv lock`
  - Create `src/mcclub_rag/discordbot/__init__.py` with a module docstring stating that nothing in the
    package imports `ingest/`, `retrieval/` or `llm/`
  - Confirm `uv lock --check`, ruff and the fast pytest job are still green
  - _Requirements: 9.3, 9.4_

- [x] 2. Settings
- [x] 2.1 Create `discordbot/settings.py` with `BotSettings`, `get_bot_settings()` and the nine fields
  - `env_prefix="DISCORD_"`, `env_file=".env"`, `extra="ignore"`, explicit aliases for
    `MEMBER_TOKEN` and `LOG_LEVEL`
  - `bot_token` required and non-empty, so an unfilled placeholder fails at startup with a clear message
  - A blank `DISCORD_X=` line is "unset", not a validation error, so the blank `DISCORD_GUILD_ID=` /
    `DISCORD_MEMBER_ROLE_ID=` that `.env.example` ships keep their documented meaning (added
    2026-10-16 after the first live run crashed on them; see EDGE_CASES.md, `_blank_means_unset`)
  - Tests: each `DISCORD_` name maps to its field, the two aliases bypass the prefix, an empty token
    raises, a full app `.env` is tolerated without error, every optional field falls back to its
    default on a blank *and* on a whitespace-only value, and the shipped `.env.example` boots
  - _Requirements: 8.1, 8.2, 8.3_

- [x] 3. Localized notices
- [x] 3.1 Create `discordbot/notices.py` with the en/fr/ar string table, `Notice`, and
  `notice_language()`
  - Reuse the wording of `messages.NO_ANSWER` and `messages.UNAVAILABLE` rather than inventing a
    second voice for the same situation
  - Tests: Arabic-script, French and English questions pick their language; an unclassifiable string
    falls back to English; every notice key exists in all three languages
  - _Requirements: 7.2_

- [x] 4. In-memory state
- [x] 4.1 Create `discordbot/state.py` with `ConversationDirectory`, `UserCooldown` and `GuildSlots`
  - `UserCooldown` takes an injectable clock so its window is testable without sleeping
  - Tests: remember/get/forget round-trip, two guilds with the same channel id stay separate, a
    cooldown returns the remaining seconds and then clears, a guild at its cap is refused and a
    released slot is reusable
  - _Requirements: 4.1, 4.3, 4.4, 6.1_

- [x] 5. Rendering
- [x] 5.1 Create `discordbot/render.py` with `Message`, `EmbedSpec`, `FieldSpec`, `RenderedReply`
  and `render_answer`
  - Embed description capped at 4000 chars; continuation messages split at paragraph then word
    boundaries under 1900 chars, in order, nothing dropped
  - Grounding label when `grounded` is false, sources as `title` + `snippet` fields, usage footer,
    green/grey colour by grounding
  - Tests: a short answer is one part; a long answer splits in order with no character lost and a `…`
    on every part that is followed by another; `doc_id` never appears; an RTL answer comes back
    byte-for-byte; `public` adds the attribution line
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [x] 6. API client
- [x] 6.1 Create `discordbot/client.py` with `RagClient`, `ChatOutcome`, `ApiFailure` and
  `HealthOutcome`
  - `ask`, `feedback`, `health`, `aclose`; one `httpx.AsyncClient` with the configured timeout and an
    injectable transport
  - Bearer header only when `as_member` and a token exist; `conversation_id` omitted when `None`
  - Tests over `httpx.MockTransport`: header present/absent, body shape, response parsing, and the
    `401`/`422`/`429`/`503`/`500`/connect/timeout mapping into `ApiFailure` codes
  - _Requirements: 3.1, 3.3, 6.2, 7.1_

- [x] 7. Command logic
- [x] 7.1 Create `discordbot/commands.py` with `AskRequest`, `precheck`, `run_ask`, `run_reset`
  and `run_status`
  - `precheck` is synchronous and refuses an over-long question and a caller inside cooldown or at the
    guild cap without touching the network; a blank question passes through
  - `run_ask` omits `conversation_id` when the channel has no cached id, remembers the id the API
    returned, makes exactly one call and never retries
  - Tests: over-long and blank questions, cooldown and cap refusals, first-turn omission,
    echoing, overwrite after a TTL reset, `/reset` forgetting, `/status` output, and the failure-code
    to notice mapping for `unauthorized`, `rate_limited`, `index_unavailable`, `validation_error`,
    `unreachable`, `timeout`
  - _Requirements: 1.4, 1.5, 3.3, 4.1, 4.2, 4.4, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2_

- [x] 8. Feedback buttons
- [x] 8.1 Create `discordbot/views.py` with `FeedbackView` and the custom-id round-trip helpers
  - Buttons encode the conversation id and the rating in the `custom_id`, so nothing has to be
    remembered; a press calls `feedback` once and replaces the view with only the pressed button,
    disabled. Discord cannot re-dispatch such a press after a bot restart (the id set is unbounded),
    which the code and design.md state as a known limitation
  - Tests: custom id round-trips, a press calls the API once, a second press cannot be counted, a
    failing call leaves the answer untouched and still answers the interaction
  - _Requirements: 5.1, 5.2, 5.3_

- [x] 9. Discord wiring
- [x] 9.1 Create `discordbot/app.py` with the `CommandTree`, `is_member`, `build_client` and the three
  command handlers
  - Default intents only, no `on_message`; `is_member` requires the configured guild, a
    `discord.Member` and the configured role
  - Handler order: `precheck` -> refuse without deferring -> `defer(ephemeral=not public)` -> `run_ask`
    -> `edit_original_response` for part 0, `followup.send` for the rest -> attach the view to the last
    part; `GuildSlots` released in a `finally`
  - Guild-scoped sync when `DISCORD_GUILD_ID` is set, global otherwise, with `copy_global_to` before
    the guild sync: without it discord.py sends an empty payload and registers nothing (found live
    2026-10-16; see EDGE_CASES.md)
  - Tests through `tests/bot_fakes.py`: defer precedes the client call, part 0 edits the original, the
    rest are follow-ups, the view lands on the last part, parts are ephemeral unless `public`, the role
    check gates the Bearer header, and a failure code produces one ephemeral notice
  - _Requirements: 1.1, 1.2, 1.3, 1.6, 2.5, 3.1, 3.2, 7.3, 7.4_

- [x] 10. Entry point
- [x] 10.1 Create `discordbot/__main__.py` running `configure_logging`, `BotSettings`, the gateway
  client and a clean `aclose` on shutdown, plus `bot_ask_done` / `bot_ask_failed` /
  `bot_ask_rejected` log events that never carry the question, the answer or the token
  - Tests with `capture_logs()`: one event per interaction, and no record contains the token or the
    question text
  - _Requirements: 7.5_

- [x] 11. Deployment
- [x] 11.1 Add the `bot` service to `docker-compose.yml`: same image, `command: python -m
  mcclub_rag.discordbot`, `env_file: .env`, `depends_on: app: {condition: service_healthy}`,
  `healthcheck: {disable: true}`, and no `ports` or volumes
  - Test: parse the file and assert the service exists, publishes no ports, and starts after the app
  - _Requirements: 9.1, 9.2_

- [x] 12. Configuration documentation
- [x] 12.1 Add the `DISCORD_*` variables to `.env.example`, one comment line each, in the file's
  existing style
  - Test: every `BotSettings` field without an alias has a line in `.env.example`, so the two cannot
    drift
  - _Requirements: 8.1_

- [x] 13. Integration
- [x] 13.1 Add the end-to-end wiring test: the real `RagClient` over `httpx.MockTransport` into
  `run_ask`, through `FakeInteraction`, asserting the `precheck -> defer -> one call -> edit` sequence
  and the rendered answer in one pass
  - Run `uv lock --check`, `ruff check .`, `ruff format --check .` and the fast pytest job
  - _Requirements: 1.3, 7.1, 10.1, 10.2_
