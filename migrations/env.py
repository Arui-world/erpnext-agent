from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from erpnext_agent.config import get_settings
from erpnext_agent.db import Base, load_models

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_models()
target_metadata = Base.metadata
_MANAGED_TABLES = frozenset(target_metadata.tables)

# Two signed 32-bit keys spelling an application-specific namespace. A session-level
# PostgreSQL advisory lock serializes competing deployment jobs across service instances.
_MIGRATION_LOCK_KEYS = (1163022414, 1095189838)


def _database_url() -> str:
    configured = config.attributes.get("database_url")
    return str(configured) if configured else get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.execute(
                text("SELECT pg_advisory_lock(:namespace, :application)"),
                {
                    "namespace": _MIGRATION_LOCK_KEYS[0],
                    "application": _MIGRATION_LOCK_KEYS[1],
                },
            )
            await connection.commit()
            try:
                await connection.run_sync(_run_migrations)
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :application)"),
                    {
                        "namespace": _MIGRATION_LOCK_KEYS[0],
                        "application": _MIGRATION_LOCK_KEYS[1],
                    },
                )
    finally:
        await connectable.dispose()


def _include_name(
    name: str | None,
    type_: str,
    parent_names: dict[str, str | None],
) -> bool:
    """Keep Alembic drift checks away from deliberately unmanaged legacy tables."""

    if type_ == "table":
        return bool(name in _MANAGED_TABLES)
    table_name = parent_names.get("table_name")
    return table_name is None or table_name in _MANAGED_TABLES


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async_migrations())
