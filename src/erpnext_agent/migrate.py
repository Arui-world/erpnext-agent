from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from erpnext_agent.config import Settings, get_settings
from erpnext_agent.db import create_engine, verify_database_revision


def alembic_config(settings: Settings) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    # Config attributes are process-local and do not serialize the database URL to disk.
    config.attributes["database_url"] = settings.database_url
    return config


async def check_database(settings: Settings) -> str:
    engine = create_engine(settings.database_url)
    try:
        return await verify_database_revision(engine)
    finally:
        await engine.dispose()


def upgrade(settings: Settings) -> str:
    command.upgrade(alembic_config(settings), "head")
    return asyncio.run(check_database(settings))


def check(settings: Settings) -> str:
    revision = asyncio.run(check_database(settings))
    command.check(alembic_config(settings))
    return revision


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the ERPNext Agent database schema")
    parser.add_argument("command", choices=("upgrade", "check", "current"))
    args = parser.parse_args()
    settings = get_settings()

    if args.command == "upgrade":
        revision = upgrade(settings)
        print(f"Database upgraded to {revision}")
    elif args.command == "check":
        revision = check(settings)
        print(f"Database revision and ORM metadata are current: {revision}")
    else:
        command.current(alembic_config(settings), verbose=False)


if __name__ == "__main__":
    main()
