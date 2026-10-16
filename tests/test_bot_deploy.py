"""Deployment: the bot runs beside the stack, with no port and no model (Req 9.1, 9.2, 9.4)."""

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = _ROOT / "docker-compose.yml"
_PACKAGE = _ROOT / "src" / "mcclub_rag" / "discordbot"

# Modules that would drag the embedder, the reranker or the parser into the bot process.
_MODEL_MODULES = ("mcclub_rag.ingest", "mcclub_rag.retrieval", "mcclub_rag.llm")


def _services() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def test_the_bot_is_its_own_service() -> None:
    bot = _services()["bot"]
    assert bot["command"] == ["python", "-m", "mcclub_rag.discordbot"]
    assert bot["env_file"] == ".env"
    assert bot["restart"] == "unless-stopped"


def test_the_entry_point_the_service_runs_exists() -> None:
    assert (_PACKAGE / "__main__.py").is_file()


def test_the_bot_publishes_no_port_and_mounts_nothing() -> None:
    """Req 9.2: it dials out to Discord's gateway, so it needs no inbound port and no volume."""
    bot = _services()["bot"]
    assert "ports" not in bot
    assert "volumes" not in bot


def test_the_bot_waits_for_a_healthy_app_and_is_not_probed_itself() -> None:
    bot = _services()["bot"]
    assert bot["depends_on"]["app"]["condition"] == "service_healthy"
    # The image's healthcheck curls :8000, which the bot does not serve.
    assert bot["healthcheck"] == {"disable": True}


def test_the_existing_stack_is_unchanged() -> None:
    services = _services()
    assert set(services) == {"qdrant", "app", "bot"}
    assert services["app"]["ports"] == ["8000:8000"]
    assert services["app"]["depends_on"] == ["qdrant"]
    assert services["qdrant"]["image"].startswith("qdrant/qdrant")


def test_the_bot_package_imports_no_model_bearing_module() -> None:
    """Req 9.4, checked on the source: importing a model stack is what it must never do."""
    for path in sorted(_PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for module in _MODEL_MODULES:
            assert f"from {module}" not in source, (path.name, module)
            assert f"import {module}" not in source, (path.name, module)
