"""Bot settings, separate from the app's ``Settings`` so neither side edits the other (Req 8).

The bot reads the same ``.env`` the app reads, so ``extra="ignore"`` keeps the app's forty-odd
settings from failing validation here, and the variables the bot shares with the app
(``MEMBER_TOKEN``, ``ADMIN_API_KEY``, ``LOG_LEVEL``) bypass the ``DISCORD_`` prefix through
``validation_alias``.

Access is per guild (Req 16). A Discord role id belongs to exactly one guild, so one guild id plus
one member role plus one admin role could only ever describe one server; both roles are read from
the entry of the guild an interaction came from, and a guild that is not listed grants nothing.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_RAG_BASE_URL = "http://app:8000"

# `guild_id:member_role_id:admin_role_id`, the two role fields optional (Req 16.1).
_GRAMMAR = "guild_id:member_role_id:admin_role_id"
_ENTRY_FIELDS = 3


@dataclass(frozen=True)
class GuildAccess:
    """What one Discord server may do (Req 16)."""

    guild_id: int
    member_role_id: int | None = None
    admin_role_id: int | None = None


def _id(entry: str, field: str, kind: str) -> int:
    try:
        return int(field)
    except ValueError:
        raise ValueError(
            f"DISCORD_GUILDS entry {entry!r}: {kind} {field!r} is not a Discord id"
        ) from None


def _optional_id(entry: str, fields: list[str], index: int, kind: str) -> int | None:
    if index >= len(fields) or not fields[index].strip():
        return None
    return _id(entry, fields[index].strip(), kind)


def parse_guild_entry(entry: str) -> GuildAccess:
    """One `guild_id[:member_role_id[:admin_role_id]]` entry (Req 16.1)."""
    fields = entry.split(":")
    if len(fields) > _ENTRY_FIELDS:
        raise ValueError(
            f"DISCORD_GUILDS entry {entry!r}: expected {_GRAMMAR}, "
            f"so at most {_ENTRY_FIELDS} colon-separated fields"
        )
    guild_field = fields[0].strip()
    if not guild_field:
        raise ValueError(f"DISCORD_GUILDS entry {entry!r}: the guild id is empty")
    return GuildAccess(
        guild_id=_id(entry, guild_field, "the guild id"),
        member_role_id=_optional_id(entry, fields, 1, "member_role_id"),
        admin_role_id=_optional_id(entry, fields, 2, "admin_role_id"),
    )


def parse_guilds(value: Any) -> Any:
    """The `DISCORD_GUILDS` grammar (Req 16.1).

    Raises on a malformed or duplicated entry, naming it: a typo in an access rule must fail at
    startup rather than silently grant or withhold access (Req 16.3).
    """
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, (tuple, list, set, frozenset)):
        items = list(value)
    else:
        return value  # let pydantic report the type itself
    entries: list[GuildAccess] = []
    for item in items:
        if isinstance(item, str):
            if not item.strip():  # a trailing comma is a typo, not a guild
                continue
            item = parse_guild_entry(item.strip())
        if not isinstance(item, GuildAccess):
            return value
        entries.append(item)
    seen: set[int] = set()
    for entry in entries:
        if entry.guild_id in seen:
            raise ValueError(
                f"DISCORD_GUILDS lists guild {entry.guild_id} twice, "
                "so its access rule would be ambiguous"
            )
        seen.add(entry.guild_id)
    return tuple(entries)


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DISCORD_", env_file=".env", extra="ignore")

    # Required and non-empty: an unfilled DISCORD_BOT_TOKEN= must fail at startup, not at the first
    # gateway login (the same fail-fast posture as ingest/ocr.verify_tessdata).
    bot_token: str = Field(min_length=1)
    rag_base_url: str = DEFAULT_RAG_BASE_URL
    # NoDecode: the grammar above is not JSON, and pydantic-settings JSON-decodes a complex field in
    # the source, before any validator runs, so the tag is what lets the parser below see a string.
    guilds: Annotated[tuple[GuildAccess, ...], NoDecode] = ()
    user_cooldown_s: float = Field(default=20.0, ge=0)
    max_concurrency: int = Field(default=4, gt=0)
    api_timeout_s: float = Field(default=60.0, gt=0)
    # An ingest parses, OCRs and embeds inside the one request, so the chat budget would cut off
    # every real scan; 15 minutes matches the pipeline's own 10 s/page OCR allowance.
    admin_timeout_s: float = Field(default=900.0, gt=0)
    member_token: str | None = Field(default=None, validation_alias="MEMBER_TOKEN")
    admin_api_key: str | None = Field(default=None, validation_alias="ADMIN_API_KEY")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    @field_validator("guilds", mode="before")
    @classmethod
    def _guilds_from_the_environment(cls, value: Any) -> Any:
        return parse_guilds(value)

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, data: Any) -> Any:
        """A `DISCORD_X=` line means "unset", so the field keeps its default.

        `.env.example` ships `DISCORD_GUILDS=` blank on purpose, which is how Req 16.2 says "no
        guild configured: every question public and no admin". A present-but-empty variable is not
        an absent one: pydantic-settings passes `""` straight through, and Docker's `env_file` does
        the same, so without this the shipped default was a startup ValidationError on both the
        local and the Compose path (EDGE_CASES.md).

        `bot_token` is exempt: Req 8.2 wants an unfilled token to fail fast instead.
        """
        if not isinstance(data, dict):
            return data
        return {
            key: value
            for key, value in data.items()
            if key == "bot_token" or not (isinstance(value, str) and not value.strip())
        }

    def access_for(self, guild_id: int | None) -> GuildAccess | None:
        """The rule for this guild, or `None` for anything outside the mapping (Req 16.4).

        `None` is the answer for a DM, for a guild nobody listed, and for the no-guilds case, which
        is what keeps membership and admin rights from being inherited across servers.
        """
        if guild_id is None:
            return None
        for entry in self.guilds:
            if entry.guild_id == guild_id:
                return entry
        return None

    @property
    def guild_ids(self) -> tuple[int, ...]:
        """The configured guilds, for registration and for the startup log."""
        return tuple(entry.guild_id for entry in self.guilds)


@lru_cache
def get_bot_settings() -> BotSettings:
    return BotSettings()
