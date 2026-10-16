"""BotSettings: the DISCORD_ prefix, aliases, failing fast and the guild map (Req 8, 16)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcclub_rag.discordbot.settings import DEFAULT_RAG_BASE_URL, BotSettings, GuildAccess

_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _set_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")


def test_defaults_match_the_documented_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    settings = BotSettings(_env_file=None)
    assert settings.rag_base_url == DEFAULT_RAG_BASE_URL == "http://app:8000"
    assert settings.guilds == ()
    assert settings.user_cooldown_s == 20.0
    assert settings.max_concurrency == 4
    assert settings.api_timeout_s == 60.0
    assert settings.log_level == "INFO"
    assert settings.member_token is None
    assert settings.admin_api_key is None


def test_every_field_reads_its_discord_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    monkeypatch.setenv("DISCORD_RAG_BASE_URL", "http://api:9000")
    monkeypatch.setenv("DISCORD_GUILDS", "42:7:8")
    monkeypatch.setenv("DISCORD_USER_COOLDOWN_S", "5")
    monkeypatch.setenv("DISCORD_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("DISCORD_API_TIMEOUT_S", "30")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    settings = BotSettings(_env_file=None)
    assert settings.bot_token == "bot-token"
    assert settings.rag_base_url == "http://api:9000"
    assert settings.guilds == (GuildAccess(42, 7, 8),)
    assert settings.user_cooldown_s == 5.0
    assert settings.max_concurrency == 2
    assert settings.api_timeout_s == 30.0
    assert settings.admin_api_key == "admin-secret"


def test_shared_variables_bypass_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    monkeypatch.setenv("MEMBER_TOKEN", "member-secret")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DISCORD_MEMBER_TOKEN", "wrong")
    monkeypatch.setenv("DISCORD_ADMIN_API_KEY", "wrong")
    monkeypatch.setenv("DISCORD_LOG_LEVEL", "wrong")
    settings = BotSettings(_env_file=None)
    assert settings.member_token == "member-secret"
    assert settings.admin_api_key == "admin-secret"
    assert settings.log_level == "DEBUG"


def test_missing_token_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        BotSettings(_env_file=None)


def test_empty_token_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    with pytest.raises(ValidationError):
        BotSettings(_env_file=None)


def test_the_app_env_file_is_ignored(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DISCORD_BOT_TOKEN=bot-token\n"
        "OPENROUTER_API_KEY=not-ours\n"
        "INGEST_MAX_PAGES=3\n"
        "GATE_THRESHOLD=\n",
        encoding="utf-8",
    )
    settings = BotSettings(_env_file=env_file)
    assert settings.bot_token == "bot-token"


def test_env_example_documents_every_field() -> None:
    """Req 8.1: the bot's variables are documented, one comment line each. Guards against drift."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    for name, field in BotSettings.model_fields.items():
        if field.validation_alias is not None:  # shared with the app, documented there
            continue
        assert f"DISCORD_{name.upper()}=" in text, name


# `.env.example` ships these blank, and Docker's `env_file` forwards a blank line as an empty
# string rather than omitting it. An empty variable is therefore the normal way to say "unset",
# not a typo: see the incident row in EDGE_CASES.md. `bot_token` is excluded because Req 8.2 makes
# an empty token a deliberate startup failure.
_BLANK_MEANS_DEFAULT = (
    "rag_base_url",
    "guilds",
    "user_cooldown_s",
    "max_concurrency",
    "api_timeout_s",
    "log_level",
)


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("name", _BLANK_MEANS_DEFAULT)
def test_a_blank_variable_falls_back_to_its_default(
    name: str, blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace is not a value either: `DISCORD_GUILDS=   ` is still "no guilds"."""
    _set_token(monkeypatch)
    monkeypatch.setenv(f"DISCORD_{name.upper()}", blank)
    assert getattr(BotSettings(_env_file=None), name) == BotSettings.model_fields[name].default


def test_the_shipped_env_example_starts_without_a_guild(tmp_path: Path) -> None:
    """Copying `.env.example` to `.env` must boot: the guild map is blank there on purpose."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8").replace(
        "DISCORD_BOT_TOKEN=", "DISCORD_BOT_TOKEN=bot-token"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    settings = BotSettings(_env_file=env_file)
    assert settings.bot_token == "bot-token"
    assert settings.guilds == ()
    assert settings.access_for(42) is None
    assert settings.rag_base_url == DEFAULT_RAG_BASE_URL
    assert settings.user_cooldown_s == 20.0


# --- Req 16: the per-guild mapping -----------------------------------------------------------


def _mapped(value: str) -> tuple[GuildAccess, ...]:
    return BotSettings(_env_file=None, bot_token="bot-token", guilds=value).guilds


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("42:7:8", (GuildAccess(42, 7, 8),)),
        ("42:7", (GuildAccess(42, 7, None),)),
        ("42:7:", (GuildAccess(42, 7, None),)),
        ("42::8", (GuildAccess(42, None, 8),)),
        ("42", (GuildAccess(42, None, None),)),
        ("42:7,43::9", (GuildAccess(42, 7, None), GuildAccess(43, None, 9))),
        (" 42:7 , 43:8 ", (GuildAccess(42, 7, None), GuildAccess(43, 8, None))),
        ("42:7,", (GuildAccess(42, 7, None),)),  # a trailing comma is a typo, not a guild
        ("", ()),
    ],
)
def test_the_mapping_grammar(value: str, expected: tuple[GuildAccess, ...]) -> None:
    assert _mapped(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "not-a-guild:7",  # the guild id is not a snowflake
        "42:not-a-role",
        "42:7:not-an-admin",
        "42:7:8:9",  # one field too many
        ":7",  # no guild id
        "42:7,42:9",  # the same guild twice: which rule wins?
    ],
)
def test_a_malformed_mapping_fails_fast(value: str) -> None:
    """Req 16.3: a typo in an access rule must not be swallowed."""
    with pytest.raises(ValidationError):
        _mapped(value)


def test_the_error_names_the_offending_entry() -> None:
    """Naming it is the difference between a five-second and a five-minute fix."""
    with pytest.raises(ValidationError) as caught:
        _mapped("42:7,oops:8")
    assert "oops:8" in str(caught.value)


def test_a_duplicate_guild_says_which_one() -> None:
    with pytest.raises(ValidationError) as caught:
        _mapped("42:7,42:9")
    assert "42" in str(caught.value)


def test_access_is_resolved_per_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    """Req 16.4: a guild outside the mapping grants nothing, not even public-with-roles."""
    _set_token(monkeypatch)
    monkeypatch.setenv("DISCORD_GUILDS", "42:7:8,43::9")
    settings = BotSettings(_env_file=None)
    assert settings.access_for(42) == GuildAccess(42, 7, 8)
    assert settings.access_for(43) == GuildAccess(43, None, 9)
    assert settings.access_for(44) is None
    assert settings.access_for(None) is None  # a DM
    assert settings.guild_ids == (42, 43)
