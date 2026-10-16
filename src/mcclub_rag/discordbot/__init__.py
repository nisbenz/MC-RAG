"""Discord front end for the chat API (specs/discord-bot).

A thin client: one slash command reaches `POST /api/v1/chat`, the answer is rendered as a Discord
reply, and two buttons reach `POST /api/v1/feedback`.

Nothing in this package imports `ingest/`, `retrieval/` or `llm/`, so the bot loads no model file,
no ONNX runtime and no parser. The two deliberate exceptions are `logging.configure_logging`
(structlog only) and `text.lang.detect_language` for notice language; both pull nothing heavy.
"""
