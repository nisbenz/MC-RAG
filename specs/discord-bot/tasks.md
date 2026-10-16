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

- [x] 14. The guild mapping and admin identity (requirements 11, 16)
- [x] 14.1 Replace the three guild scalars with `DISCORD_GUILDS` and `GuildAccess`
  - Grammar `guild_id:member_role_id:admin_role_id`, comma-separated, an empty field means "not
    configured for this guild", trailing empty fields optional
  - A malformed entry, a wrong field count or a duplicate guild id fails at startup naming the entry,
    because a typo in an access rule is a security bug
  - `DISCORD_GUILD_ID`, `DISCORD_MEMBER_ROLE_ID` and `DISCORD_ADMIN_ROLE_ID` are removed rather than
    kept alongside, so no access rule has two sources of truth
  - `NoDecode` is required on the field: pydantic-settings JSON-decodes a complex type in the source,
    before any validator, so `42:7` reached the validator as an already-failed decode (proved with the
    installed 2.15.0; see EDGE_CASES.md)
  - Tests: `123:456`, `123:456:`, `123::789` and a two-guild value; empty means no guilds; malformed,
    non-numeric, three-field-overflow and duplicate entries refused; `access_for` for a guild outside
    the mapping returns `None`; the alias and blank-handling rules from 2.1 still hold
  - _Requirements: 8.1, 16.1, 16.2, 16.3, 16.4_

- [x] 14.2 Per-guild `is_member` / `is_admin`, and registration in every configured guild
  - Both gates resolve their guild through `access_for`, compare the guild before any role, and answer
    `False` for a guild outside the mapping, a DM, a missing role field and the no-guild case
  - `sync_commands` registers in each configured guild with `copy_global_to`, and globally when none
    are configured
  - `_admin_auth()` sends `X-Admin-Key`; the chat and feedback paths never see it
  - Tests: a role in one guild granting nothing in another, a guild with no member role staying public,
    a guild with no admin role having no admin, one registration per configured guild, the key absent
    from a chat request, and no log record containing the key or the URL
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 16.4, 16.5_

- [x] 15. Admin commands (requirements 12, 13, 14)
- [x] 15.1 Client methods and outcomes for the document routes
  - `ingest_file` (multipart), `ingest_url`, `list_documents`, `delete_document`, each returning an
    outcome carrying either the payload or an `ApiFailure`, never raising; an ingest uses
    `DISCORD_ADMIN_TIMEOUT_S` because parsing and OCR happen inside that one request
  - Tests over `httpx.MockTransport`: multipart field names and the `visibility` form value, the
    header on every admin call, `401` / `403` / `422` / `503` / unreachable mapping, `ok: false`
    distinguished from `ok: true`, and a malformed body becoming `internal` rather than a traceback
  - _Requirements: 12.1, 12.4, 13.1, 13.4, 14.3_

- [x] 15.2 Pure command logic
  - `ingest_precheck` refuses both sources and neither; `run_documents`, `run_ingest` and `run_forget`
    return `RenderedReply`; `forget_confirmation` builds the prompt the view rides on
  - Tests: both-or-neither refusals carry no API call, an empty corpus answers with the notice, a long
    listing states how many of how many, `failed` is never rendered as success, a refused key is not
    blamed on the document, and a completed delete names the id
  - _Requirements: 12.2, 12.3, 13.4, 14.2, 14.4_

- [x] 15.3 Wiring: handlers, the background ingest task and the confirmation view
  - `/documents` answers synchronously; `/ingest` defers, schedules the task, and reports by editing
    the acknowledgement, falling back to a channel message when the interaction has expired;
    `/forget` confirms through `DeleteView` and replaces the view on press
  - Tasks are held in a set so they cannot be collected mid-flight
  - The gate runs before `defer()`: a refusal costs no interaction and no API call
  - Tests through `tests/bot_fakes.py`: the gate runs first, the defer and the acknowledgement precede
    the call, an expired interaction posts to the channel and an undeliverable one is logged, one press
    deletes once, `{"ok": false}` is not a delete, and a refused call leaves the corpus alone
  - _Requirements: 13.3, 14.1_

- [x] 15.4 Notices and rendering for the new outcomes
  - `notices`: admin-only, admin-refused, no documents, one-source, ingest started/done/failed, delete
    confirm / done / not-found / cancelled, in all three languages
  - `render`: the document-listing embed (paginated, with a `showing x-y of n` footer) and the ingest
    result
  - Tests: every key exists in en/fr/ar, the listing keeps every document exactly once and stays under
    the embed cap, an empty corpus is a notice rather than an empty embed
  - _Requirements: 12.1, 12.3, 13.4, 14.3_

- [ ] 16. URL ingest on the API (requirement 15) — the last blocking gap
- [ ] 16.1 Add `ingest_url` to `DocumentBackend`, `RealDocumentBackend` and `FakeDocumentBackend`
  - The adapter calls `ingest_pipeline.ingest_url` with the parsed expiry, exactly as the file path
    calls `ingest_file`, so the SSRF guard, redirects, limits and marker ordering stay the pipeline's
  - Tests: the adapter maps `created` / `replaced` / `updated` to `indexed`, passes `topic` and the
    parsed `expires_at` through, and a bad date is a 422 before any fetch
  - _Requirements: 15.2_

- [ ] 16.2 Add `POST /api/v1/documents/url`
  - Admin-only, form fields `url` / `visibility` / `topic` / `expires_at`, returning `{doc_id}`;
    `_raise_for_failed_ingest` is reused so the codes and statuses match the file route
  - Tests: `401` without the key, `200` with it, the same error mapping as the file route, and the
    existing file route's tests unchanged
  - Needs eequaled's sign-off: `main.py`'s `DocumentBackend` protocol and `api/documents.py` are
    eequaled's files, so this lands as a PR they review
  - _Requirements: 15.1, 15.3, 15.4_

- [ ] 17. Documentation
- [x] 17.1 Add the `DISCORD_*` variables to `.env.example`
  - Done for the bot's own settings, including `DISCORD_GUILDS` with its grammar and the
    blank-means-unset note; the drift test passes
  - _Requirements: 8.1_
- [ ] 17.2 Correct the README API table
  - It claims `POST /api/v1/documents` takes "a file or URL"; name the file route and the URL route
    separately once 16.2 exists
  - _Requirements: 15.5_
- [ ] 17.3 Record the new surfaces in the repo docs
  - `AGENTS.md` "Discord bot" section: the six commands, the `DISCORD_GUILDS` grammar, the background
    ingest and the expired-interaction fallback
  - `EDGE_CASES.md`: the `{"doc_id"}`-only response (the bot cannot say indexed vs replaced vs
    duplicate), the `NoDecode` discovery, and the background-task-vs-15-minute-token window
  - `PROGRESS.md`: a dated entry for the admin surface
  - _Requirements: 8.1, 10.2_

- [ ] 18. Verification
- [x] 18.1 Run `uv lock --check`, `ruff check .`, `ruff format --check .` and the fast pytests
  - Green: 1337 passed, 249 skipped, 85 deselected; ruff check and format clean
  - _Requirements: 9.3, 10.1, 10.2_
- [ ] 18.2 Live guild check
  - Six commands registered, `/documents` lists the corpus, an admin refused without the role, a real
    file ingest through the API, and `/forget` deleting it again
  - Blocked until the bot is deployed with a real `DISCORD_BOT_TOKEN`, `DISCORD_GUILDS` and
    `ADMIN_API_KEY`
  - _Requirements: 10.1, 10.2_
