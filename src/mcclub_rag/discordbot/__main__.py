"""Entry point: `python -m mcclub_rag.discordbot` (the Compose bot service's command)."""

import asyncio

import structlog

from mcclub_rag.discordbot.app import build_client
from mcclub_rag.discordbot.settings import BotSettings, get_bot_settings
from mcclub_rag.logging import configure_logging

log = structlog.get_logger(__name__)


async def run(settings: BotSettings) -> None:
    client = build_client(settings)
    try:
        await client.start(settings.bot_token)
    finally:
        await client.rag_client.aclose()


def main() -> None:
    settings = get_bot_settings()
    configure_logging(settings.log_level)
    log.info("bot_starting", rag_base_url=settings.rag_base_url, guilds=list(settings.guild_ids))
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
