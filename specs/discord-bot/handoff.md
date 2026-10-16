# Discord bot: admin surface handoff

State of branch `discord-exp` at 2026-10-16, for whoever picks this up. `requirements.md` (16
requirements), `design.md` and `tasks.md` are in this directory and are the source of truth;
`tasks.md` is ticked to match this document.

## What is done (green)

- **Settings, `DISCORD_GUILDS`.** The three guild scalars were replaced by one variable,
  `DISCORD_GUILDS=guild_id:member_role_id:admin_role_id,guild_id:...`, parsed into `GuildAccess`
  entries (`src/mcclub_rag/discordbot/settings.py`). Empty field = not configured for that guild;
  `123::789` is an admin-only server. A malformed, over-long or duplicated entry stops startup with
  the offending entry in the message. The parser needs `NoDecode` on the field: pydantic-settings
  JSON-decodes a complex type in the settings *source*, before any validator, so `42:7` never
  reached the validator (proven against the installed pydantic-settings 2.15.0).
- **Per-guild gates.** `is_member` / `is_admin` (`discordbot/app.py`) resolve the guild with
  `access_for()` *before* looking at any role, and return `False` for a DM, an unlisted guild, a
  guild with the field left empty, and the no-guild case. Admin fails closed: no fallback to the
  member role or to Discord's server permissions. `sync_commands` registers in every configured
  guild and globally only when none are configured.
- **The admin commands.** `/documents` (paginated listing embed with a `showing x-y of n` footer,
  and a notice when the corpus is empty), `/ingest` (file or url, mandatory `visibility`,
  optional `topic`), `/forget` (confirmation view, then `DELETE`). All six commands are ephemeral
  for admin work, run behind the gate, and make no API call when refused.
- **`/ingest` over the 3-second window.** The gate and the both-or-neither check run before
  `defer()`; the acknowledgement is edited in, then the ingest runs in an `asyncio.Task` held in a
  module set so it cannot be collected mid-flight. When the 15-minute interaction token has died by
  the time the ingest finishes, the outcome is posted to the channel instead, so the operator is
  never unable to tell whether the corpus changed.
- **Client.** `ingest_file` (multipart), `ingest_url`, `list_documents`, `delete_document`, each
  returning an outcome or an `ApiFailure`, never raising. Ingest calls use their own
  `DISCORD_ADMIN_TIMEOUT_S` budget, because parsing and OCR happen inside the one request.
  `X-Admin-Key` rides admin calls only, never chat or feedback.
- **Tests.** 1337 passed, 249 skipped, 85 deselected (fast suite); `ruff check .` and
  `ruff format --check .` clean. 60 new bot tests cover the settings grammar, the gates, all three
  handlers, the background task and both its delivery paths, the listing renderer, the notices in
  en/fr/ar, and the confirmation view.

## What remains

### 1. `POST /api/v1/documents/url` (blocking)

`/ingest url` currently 404s: the bot's client and command layer are written against the route, but
`src/mcclub_rag/api/documents.py` has no URL route and `main.py`'s `DocumentBackend` protocol has no
`ingest_url`. `specs/discord-bot/requirements.md` requirement 15 says what it must do:

- `ingest_url` on the protocol, on `RealDocumentBackend` (delegating to
  `ingest_pipeline.ingest_url`, so the SSRF guard, redirect handling, page/size limits, docstore
  write and marker ordering stay the pipeline's), and on the test fakes.
- A new route taking form fields `url`, `visibility`, `topic`, `expires_at`, returning `{doc_id}`,
  reusing `_raise_for_failed_ingest` so the codes and statuses match the file route. A separate
  route, not a new field: the file route's `file` is required, so "exactly one of file or url"
  cannot be expressed in one signature.
- The existing file route's contract must not change, and its tests must stay untouched.

**These are eequaled's files** (`api/`, `main.py`'s protocol, `schemas.py`). Per the seam rules in
`AGENTS.md`, land it as its own PR that eequaled reviews, and do not change a frozen signature
without saying so.

### 2. Docs

- `README.md`: the API table claims `POST /api/v1/documents` takes "a file or URL". Name the two
  routes separately, once the URL route exists.
- `AGENTS.md`, "Discord bot" section: the six commands, the `DISCORD_GUILDS` grammar, the background
  ingest, the expired-interaction fallback.
- `EDGE_CASES.md`: three rows, each with its proof —
  - `POST /api/v1/documents` answers `{doc_id}` and nothing else, so the bot cannot report indexed
    vs replaced vs duplicate (`DocumentCreateResponse`; one-line API change would fix it);
  - the `NoDecode` requirement above, with the failing probe;
  - an ingest that outlives the 15-minute interaction token, and the channel-post fallback.
- `PROGRESS.md`: a dated entry.

### 3. Live check (needs a real deployment)

Six commands registered in the guild, `/documents` listing the corpus, `/ask` still working, an
admin refused without the role, a real file ingest through the API, and `/forget` deleting it.
Needs `DISCORD_BOT_TOKEN`, `DISCORD_GUILDS` and `ADMIN_API_KEY` in `.env`. Not run yet.

## Verification

```bash
uv lock --check
uv run ruff check . && uv run ruff format --check .
uv run pytest -m "not ocr and not tokenizer and not embedder and not reranker"
```

## Open questions for the user

1. **Should `/ingest`'s `visibility` stay mandatory?** It is required today, on purpose: a default
   would let an operator publish members-only material by forgetting the option. An env default
   (`DISCORD_DEFAULT_VISIBILITY`) is the alternative.
2. **Is a third "admin-only knowledge" tier wanted?** The API has exactly two visibilities,
   `public` and `members`. Admin is a *capability* (ingest / forget / list), not a visibility, so
   an admin-only corpus would be an API and retrieval change, not a bot change.
