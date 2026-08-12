from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

EXPECTED_DATABASE_REVISION = "20260812_0002"
MANAGED_DATABASE_TABLES = frozenset(
    {"actions", "chat_conversations", "chat_messages", "oauth_device_credentials"}
)


class Base(DeclarativeBase):
    pass


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def load_models() -> None:
    """Import every mapped model before Alembic inspects Base.metadata."""

    from erpnext_agent.actions import models as action_models  # noqa: F401
    from erpnext_agent.auth import models as auth_models  # noqa: F401
    from erpnext_agent.conversations import models as conversation_models  # noqa: F401


class DatabaseRevisionError(RuntimeError):
    """The application database has not been migrated to the expected revision."""


async def verify_database_revision(engine: AsyncEngine) -> str:
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: frozenset(inspect(sync_connection).get_table_names())
            )
            revisions = list(
                (
                    await connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).scalars()
            )
    except SQLAlchemyError as exc:
        raise DatabaseRevisionError(
            "Database migration metadata is unavailable; run the migration service"
        ) from exc
    missing_tables = MANAGED_DATABASE_TABLES - tables
    if missing_tables:
        raise DatabaseRevisionError(
            "Database managed tables are incomplete; run the migration service"
        )
    if revisions != [EXPECTED_DATABASE_REVISION]:
        raise DatabaseRevisionError(
            "Database revision does not match the application; run the migration service"
        )
    return str(revisions[0])


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
