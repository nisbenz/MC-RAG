"""Bot settings, separate from the app's ``Settings`` so neither side edits the other (Req 8).

The bot reads the same ``.env`` the app reads, so ``extra="ignore"`` keeps the app's forty-odd
settings from failing validation here, and the two variables the bot shares with the app
(``MEMBER_TOKEN``, ``LOG_LEVEL``) bypass the ``DISCORD_`` prefix through ``validation_alias``.
"""

from functools import lru_cache
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_RAG_BASE_URL = "http://app:8000"


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DISCORD_", env_file=".env", extra="ignore")

    # Required and non-empty: an unfilled DISCORD_BOT_TOKEN= must fail at startup, not at the first
    # gateway login (the same fail-fast posture as ingest/ocr.verify_tessdata).
    bot_token: str = Field(min_length=1)
    rag_base_url: str = DEFAULT_RAG_BASE_URL
    guild_id: int | None = None
    member_role_id: int | None = None
    user_cooldown_s: float = Field(default=20.0, ge=0)
    max_concurrency: int = Field(default=4, gt=0)
    api_timeout_s: float = Field(default=60.0, gt=0)
    member_token: str | None = Field(default=None, validation_alias="MEMBER_TOKEN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """A `DISCORD_X=` line means "unset", so the field keeps its default.

        `.env.example` ships `DISCORD_GUILD_ID=` and `DISCORD_MEMBER_ROLE_ID=` blank on purpose,
        which is how Reqs 1.1 and 3.2 say "register globally" and "every question is public". A
        present-but-empty variable is not an absent one: pydantic-settings passes `""` straight
        through, and Docker's `env_file` does the same, so without this the shipped defaults were a
        startup ValidationError on both the local and the Compose path (EDGE_CASES.md).

        `bot_token` is exempt: Req 8.2 wants an unfilled token to fail fast instead.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if key == "bot_token" or not (isinstance(value, str) and not value.strip())
        }


@lru_cache
def get_bot_settings() -> BotSettings:
    return BotSettings()
