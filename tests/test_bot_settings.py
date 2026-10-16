"""BotSettings: the DISCORD_ prefix, the two shared aliases, and failing fast (Req 8)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcclub_rag.discordbot.settings import DEFAULT_RAG_BASE_URL, BotSettings

_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _set_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")


def test_defaults_match_the_documented_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    settings = BotSettings(_env_file=None)
    assert settings.rag_base_url == DEFAULT_RAG_BASE_URL == "http://app:8000"
    assert settings.guild_id is None
    assert settings.member_role_id is None
    assert settings.user_cooldown_s == 20.0
    assert settings.max_concurrency == 4
    assert settings.api_timeout_s == 60.0
    assert settings.log_level == "INFO"
    assert settings.member_token is None


def test_every_field_reads_its_discord_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    monkeypatch.setenv("DISCORD_RAG_BASE_URL", "http://api:9000")
    monkeypatch.setenv("DISCORD_GUILD_ID", "42")
    monkeypatch.setenv("DISCORD_MEMBER_ROLE_ID", "7")
    monkeypatch.setenv("DISCORD_USER_COOLDOWN_S", "5")
    monkeypatch.setenv("DISCORD_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("DISCORD_API_TIMEOUT_S", "30")
    settings = BotSettings(_env_file=None)
    assert settings.bot_token == "bot-token"
    assert settings.rag_base_url == "http://api:9000"
    assert settings.guild_id == 42
    assert settings.member_role_id == 7
    assert settings.user_cooldown_s == 5.0
    assert settings.max_concurrency == 2
    assert settings.api_timeout_s == 30.0


def test_shared_variables_bypass_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_token(monkeypatch)
    monkeypatch.setenv("MEMBER_TOKEN", "member-secret")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DISCORD_MEMBER_TOKEN", "wrong")
    monkeypatch.setenv("DISCORD_LOG_LEVEL", "wrong")
    settings = BotSettings(_env_file=None)
    assert settings.member_token == "member-secret"
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
    "guild_id",
    "member_role_id",
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
    """Whitespace is not a value either: `DISCORD_GUILD_ID=   ` is still "no guild"."""
    _set_token(monkeypatch)
    monkeypatch.setenv(f"DISCORD_{name.upper()}", blank)
    assert getattr(BotSettings(_env_file=None), name) == BotSettings.model_fields[name].default


def test_the_shipped_env_example_starts_without_a_guild(tmp_path: Path) -> None:
    """Copying `.env.example` to `.env` must boot: guild and role are blank there on purpose."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8").replace(
        "DISCORD_BOT_TOKEN=", "DISCORD_BOT_TOKEN=bot-token"
    )
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    settings = BotSettings(_env_file=env_file)
    assert settings.bot_token == "bot-token"
    assert settings.guild_id is None
    assert settings.member_role_id is None
    assert settings.rag_base_url == DEFAULT_RAG_BASE_URL
    assert settings.user_cooldown_s == 20.0
