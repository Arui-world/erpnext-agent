from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from erpnext_agent.config import Settings
from erpnext_agent.db import (
    EXPECTED_DATABASE_REVISION,
    MANAGED_DATABASE_TABLES,
    Base,
    load_models,
)

PROJECT_ROOT = Path(__file__).parents[2]


def test_alembic_has_one_expected_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [EXPECTED_DATABASE_REVISION]


def test_managed_tables_match_current_orm_metadata() -> None:
    load_models()

    assert frozenset(Base.metadata.tables) == MANAGED_DATABASE_TABLES
    assert "oauth_credentials" not in MANAGED_DATABASE_TABLES
    assert "oauth_device_credentials" in MANAGED_DATABASE_TABLES


def test_runtime_no_longer_accepts_create_all_configuration() -> None:
    assert "auto_create_schema" not in Settings.model_fields
    assert "AUTO_CREATE_SCHEMA" not in (PROJECT_ROOT / ".env.example").read_text()


def test_compose_gates_agent_on_successful_migration() -> None:
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert 'command: ["python", "-m", "erpnext_agent.migrate", "upgrade"]' in compose
    assert "service_completed_successfully" in compose
