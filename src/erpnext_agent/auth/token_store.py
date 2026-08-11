from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erpnext_agent.auth.models import OAuthCredentialRecord
from erpnext_agent.auth.oauth_client import OAuthTokenSet


class CredentialNotFoundError(LookupError):
    pass


class CredentialDecryptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredCredential:
    credential_id: str
    site: str
    oauth_subject: str
    user_id: str
    access_token: str
    refresh_token: str | None
    scope: str
    expires_at: datetime | None
    updated_at: datetime
    revoked_at: datetime | None


class TokenStore:
    def __init__(self, *, encryption_key: str, key_version: str, client_id: str) -> None:
        self._fernet = Fernet(encryption_key.encode())
        self._key_version = key_version
        self._client_id = client_id

    async def upsert(
        self,
        session: AsyncSession,
        *,
        site: str,
        oauth_subject: str,
        user_id: str,
        token: OAuthTokenSet,
    ) -> str:
        record = await session.scalar(
            select(OAuthCredentialRecord).where(
                OAuthCredentialRecord.site == site,
                OAuthCredentialRecord.user_id == user_id,
                OAuthCredentialRecord.client_id == self._client_id,
            )
        )
        now = datetime.now(UTC)
        if record is None:
            record = OAuthCredentialRecord(
                credential_id=str(uuid.uuid4()),
                site=site,
                client_id=self._client_id,
                oauth_subject=oauth_subject,
                user_id=user_id,
                access_token_ciphertext="",
                key_version=self._key_version,
                created_at=now,
                updated_at=now,
            )
            session.add(record)

        record.oauth_subject = oauth_subject
        record.scope = token.scope
        record.token_type = token.token_type
        record.access_token_ciphertext = self._encrypt(token.access_token)
        if token.refresh_token:
            record.refresh_token_ciphertext = self._encrypt(token.refresh_token)
        record.expires_at = now + timedelta(seconds=token.expires_in) if token.expires_in else None
        record.key_version = self._key_version
        record.updated_at = now
        record.revoked_at = None
        await session.flush()
        return record.credential_id

    async def get(self, session: AsyncSession, credential_id: str) -> StoredCredential:
        record = await session.scalar(
            select(OAuthCredentialRecord)
            .where(OAuthCredentialRecord.credential_id == credential_id)
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise CredentialNotFoundError(credential_id)
        return self._stored_credential(record)

    async def get_for_user(
        self,
        session: AsyncSession,
        *,
        site: str,
        user_id: str,
    ) -> StoredCredential:
        """Resolve the encrypted credential for a persisted Action owner."""

        record = await session.scalar(
            select(OAuthCredentialRecord)
            .where(
                OAuthCredentialRecord.site == site,
                OAuthCredentialRecord.user_id == user_id,
                OAuthCredentialRecord.client_id == self._client_id,
            )
            .execution_options(populate_existing=True)
        )
        if record is None:
            raise CredentialNotFoundError(f"{site}:{user_id}")
        return self._stored_credential(record)

    def _stored_credential(self, record: OAuthCredentialRecord) -> StoredCredential:
        return StoredCredential(
            credential_id=record.credential_id,
            site=record.site,
            oauth_subject=record.oauth_subject,
            user_id=record.user_id,
            access_token=self._decrypt(record.access_token_ciphertext),
            refresh_token=(
                self._decrypt(record.refresh_token_ciphertext)
                if record.refresh_token_ciphertext
                else None
            ),
            scope=record.scope,
            expires_at=record.expires_at,
            updated_at=record.updated_at,
            revoked_at=record.revoked_at,
        )

    async def revoke(self, session: AsyncSession, credential_id: str) -> None:
        record = await session.get(OAuthCredentialRecord, credential_id)
        if record is not None:
            record.revoked_at = datetime.now(UTC)
            record.access_token_ciphertext = self._encrypt("")
            record.refresh_token_ciphertext = None
            record.updated_at = datetime.now(UTC)

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise CredentialDecryptError("Unable to decrypt the stored OAuth credential") from exc
