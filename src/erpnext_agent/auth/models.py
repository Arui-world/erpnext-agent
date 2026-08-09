from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from erpnext_agent.db import Base


class OAuthCredentialRecord(Base):
    __tablename__ = "oauth_credentials"
    __table_args__ = (
        UniqueConstraint("site", "user_id", "client_id", name="uq_oauth_user_client"),
    )

    credential_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    site: Mapped[str] = mapped_column(String(255), index=True)
    client_id: Mapped[str] = mapped_column(String(255))
    oauth_subject: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    scope: Mapped[str] = mapped_column(Text, default="")
    token_type: Mapped[str] = mapped_column(String(32), default="Bearer")
    access_token_ciphertext: Mapped[str] = mapped_column(Text)
    refresh_token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    key_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

