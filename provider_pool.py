"""Database-backed provider profiles and user preferences.

Provider API keys are encrypted with Fernet before being written to SQLite or
PostgreSQL. The encryption key is supplied through API_KEY_ENCRYPTION_KEY or the
legacy PROVIDER_STORE_KEY variable; when neither is set, a stable key is derived
from BOT_TOKEN so the bot can be configured from Telegram without another secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
ALLOWED_RESPONSE_FORMATS = {"auto", "json_schema", "json_object", "none"}
ALLOWED_TOKEN_PARAMS = {"auto", "max_tokens", "max_completion_tokens"}
ALLOWED_REASONING = {"", "minimal", "low", "medium", "high"}
ALLOWED_PREFERENCE_KEYS = {"language", "default_niche", "style"}


class ProviderPoolError(RuntimeError):
    """Raised when provider storage or a provider profile cannot be used safely."""


@dataclass
class ProviderProfile:
    name: str
    api_key: str
    base_url: str
    model: str
    response_format: str = "auto"
    max_tokens_param: str = "auto"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    reasoning_effort: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProviderProfile":
        return cls(
            name=str(raw.get("name", "")),
            api_key=str(raw.get("api_key", "")),
            base_url=str(raw.get("base_url", "")),
            model=str(raw.get("model", "")),
            response_format=str(raw.get("response_format", "auto")),
            max_tokens_param=str(raw.get("max_tokens_param", "auto")),
            timeout_seconds=float(raw.get("timeout_seconds", 60.0)),
            max_retries=int(raw.get("max_retries", 2)),
            reasoning_effort=str(raw.get("reasoning_effort", "")),
        )

    def validate(self) -> None:
        if not NAME_PATTERN.fullmatch(self.name):
            raise ProviderPoolError(
                "Provider name must be 1-40 characters using letters, numbers, '.', '_' or '-'."
            )
        if not self.api_key.strip():
            raise ProviderPoolError("API key cannot be empty")
        if not self.model.strip():
            raise ProviderPoolError("Model cannot be empty")
        if self.response_format not in ALLOWED_RESPONSE_FORMATS:
            raise ProviderPoolError("Invalid response format")
        if self.max_tokens_param not in ALLOWED_TOKEN_PARAMS:
            raise ProviderPoolError("Invalid token parameter mode")
        if self.reasoning_effort not in ALLOWED_REASONING:
            raise ProviderPoolError("Invalid reasoning effort")
        if self.timeout_seconds < 10 or self.timeout_seconds > 600:
            raise ProviderPoolError("Timeout must be between 10 and 600 seconds")
        if self.max_retries < 0 or self.max_retries > 10:
            raise ProviderPoolError("Retries must be between 0 and 10")


class Base(DeclarativeBase):
    pass


class ProviderRecord(Base):
    __tablename__ = "provider_profiles"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_provider_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    response_format: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    max_tokens_param: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    timeout_seconds: Mapped[float] = mapped_column(nullable=False, default=60.0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    reasoning_effort: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserPreferenceRecord(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="English")
    default_niche: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    style: Mapped[str] = mapped_column(String(100), nullable=False, default="professional")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChannelProfileRecord(Base):
    __tablename__ = "channel_profiles"
    __table_args__ = (UniqueConstraint("owner_user_id", "chat_id", name="uq_channel_owner_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    signature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TemplateRecord(Base):
    __tablename__ = "content_templates"
    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_template_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    cta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLogRecord(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    draft_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    batch_id: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class RecurringPostRecord(Base):
    __tablename__ = "recurring_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    draft_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DraftRecord(Base):
    __tablename__ = "content_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    post_text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    cta: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_facts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", index=True)
    channel_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_type: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    media_file_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    buttons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    link_preview_disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    watermark_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderPoolStore:
    """Persistent provider profiles and user preferences for SQLite or PostgreSQL."""

    def __init__(self, database_url: str, encryption_key: str) -> None:
        if not database_url:
            raise ProviderPoolError("DATABASE_URL cannot be empty")
        if not encryption_key:
            raise ProviderPoolError("API_KEY_ENCRYPTION_KEY is required for database storage")
        try:
            self.fernet = Fernet(encryption_key.encode())
        except Exception as exc:
            raise ProviderPoolError(
                "API_KEY_ENCRYPTION_KEY must be a valid Fernet key; generate one with Fernet.generate_key()."
            ) from exc

        normalized_url = self._normalize_database_url(database_url)
        engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if normalized_url.startswith("sqlite"):
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        try:
            self.engine = create_engine(normalized_url, **engine_kwargs)
            Base.metadata.create_all(self.engine)
            self._ensure_draft_media_columns()
            self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        except SQLAlchemyError as exc:
            raise ProviderPoolError(f"Database initialization failed: {exc}") from exc

    @staticmethod
    def _normalize_database_url(value: str | Path) -> str:
        url = str(value).strip()
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
            return "postgresql+psycopg://" + url[len("postgresql://") :]
        if url == "sqlite://" or url == "sqlite:///:memory:":
            return "sqlite:///:memory:"
        if url.startswith("sqlite://"):
            return url
        if "://" not in url:
            return f"sqlite:///{Path(url).expanduser()}"
        return url

    @classmethod
    def _encryption_key_from_environment(cls) -> str:
        key = (os.getenv("API_KEY_ENCRYPTION_KEY") or os.getenv("PROVIDER_STORE_KEY") or "").strip()
        if key:
            return key
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            return ""
        digest = hashlib.sha256(("telegram-provider-database:" + bot_token).encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii")

    @classmethod
    def from_environment(cls) -> "ProviderPoolStore | None":
        key = cls._encryption_key_from_environment()
        if not key:
            return None
        database_url = os.getenv("DATABASE_URL", "sqlite:///bot.db").strip()
        return cls(database_url, key)

    @staticmethod
    def mask_key(value: str) -> str:
        if len(value) <= 8:
            return "••••••••"
        return f"{value[:4]}…{value[-4:]}"

    @staticmethod
    def redact_url(value: str) -> str:
        try:
            parsed = urlsplit(value)
            sensitive = {"key", "api_key", "apikey", "token", "access_token", "secret", "authorization"}
            query = [
                (key, "[redacted]" if key.lower() in sensitive else val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        except Exception:
            return "[redacted endpoint]"

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise ProviderPoolError("A stored API key could not be decrypted") from exc

    def _to_profile(self, record: ProviderRecord) -> ProviderProfile:
        return ProviderProfile(
            name=record.name,
            api_key=self._decrypt(record.api_key_encrypted),
            base_url=record.base_url,
            model=record.model,
            response_format=record.response_format,
            max_tokens_param=record.max_tokens_param,
            timeout_seconds=record.timeout_seconds,
            max_retries=record.max_retries,
            reasoning_effort=record.reasoning_effort,
        )

    def list_profiles(self, user_id: int) -> list[dict[str, Any]]:
        with self.Session() as session:
            records = session.scalars(
                select(ProviderRecord).where(ProviderRecord.owner_user_id == int(user_id)).order_by(ProviderRecord.name)
            ).all()
            return [
                {
                    "name": record.name,
                    "active": record.is_active,
                    "api_key": self.mask_key(self._decrypt(record.api_key_encrypted)),
                    "base_url": self.redact_url(record.base_url) or "provider default",
                    "model": record.model,
                    "response_format": record.response_format,
                    "max_tokens_param": record.max_tokens_param,
                }
                for record in records
            ]

    def get(self, user_id: int, name: str | None = None) -> ProviderProfile | None:
        with self.Session() as session:
            query = select(ProviderRecord).where(ProviderRecord.owner_user_id == int(user_id))
            if name:
                query = query.where(ProviderRecord.name == name)
            else:
                query = query.where(ProviderRecord.is_active.is_(True))
            record = session.scalars(query).first()
            return self._to_profile(record) if record else None

    def upsert(self, user_id: int, profile: ProviderProfile) -> None:
        profile.validate()
        owner = int(user_id)
        now = self._now()
        with self.Session.begin() as session:
            record = session.scalars(
                select(ProviderRecord).where(
                    ProviderRecord.owner_user_id == owner,
                    ProviderRecord.name == profile.name,
                )
            ).first()
            if record is None:
                record = ProviderRecord(
                    owner_user_id=owner,
                    name=profile.name,
                    created_at=now,
                    updated_at=now,
                    is_active=False,
                )
                session.add(record)
            record.api_key_encrypted = self._encrypt(profile.api_key)
            record.base_url = profile.base_url
            record.model = profile.model
            record.response_format = profile.response_format
            record.max_tokens_param = profile.max_tokens_param
            record.timeout_seconds = profile.timeout_seconds
            record.max_retries = profile.max_retries
            record.reasoning_effort = profile.reasoning_effort
            record.updated_at = now
            if session.scalars(
                select(ProviderRecord.id).where(
                    ProviderRecord.owner_user_id == owner,
                    ProviderRecord.is_active.is_(True),
                )
            ).first() is None:
                record.is_active = True

    def activate(self, user_id: int, name: str) -> ProviderProfile:
        owner = int(user_id)
        with self.Session.begin() as session:
            records = session.scalars(select(ProviderRecord).where(ProviderRecord.owner_user_id == owner)).all()
            selected = next((record for record in records if record.name == name), None)
            if selected is None:
                raise ProviderPoolError(f"Provider '{name}' was not found for this user")
            for record in records:
                record.is_active = record.id == selected.id
                record.updated_at = self._now()
            return self._to_profile(selected)

    def remove(self, user_id: int, name: str) -> None:
        owner = int(user_id)
        with self.Session.begin() as session:
            records = session.scalars(select(ProviderRecord).where(ProviderRecord.owner_user_id == owner)).all()
            selected = next((record for record in records if record.name == name), None)
            if selected is None:
                raise ProviderPoolError(f"Provider '{name}' was not found for this user")
            was_active = selected.is_active
            session.delete(selected)
            if was_active:
                remaining = [record for record in records if record.id != selected.id]
                if remaining:
                    remaining[0].is_active = True
                    remaining[0].updated_at = self._now()

    def _ensure_draft_media_columns(self) -> None:
        columns = {column["name"] for column in inspect(self.engine).get_columns("content_drafts")}
        definitions = {
            "media_type": "VARCHAR(24) NOT NULL DEFAULT 'text'",
            "media_file_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "buttons_json": "TEXT NOT NULL DEFAULT '[]'",
            "link_preview_disabled": "BOOLEAN NOT NULL DEFAULT FALSE",
            "watermark_text": "TEXT NOT NULL DEFAULT ''",
        }
        with self.engine.begin() as connection:
            for column_name, definition in definitions.items():
                if column_name not in columns:
                    connection.execute(text(f"ALTER TABLE content_drafts ADD COLUMN {column_name} {definition}"))

    @staticmethod
    def _channel_dict(record: ChannelProfileRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_user_id": record.owner_user_id,
            "chat_id": record.chat_id,
            "label": record.label,
            "timezone": record.timezone_name,
            "signature": record.signature,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def upsert_channel_profile(self, user_id: int, chat_id: int, label: str, timezone_name: str, signature: str) -> dict[str, Any]:
        owner = int(user_id)
        now = self._now()
        with self.Session.begin() as session:
            record = session.scalars(select(ChannelProfileRecord).where(ChannelProfileRecord.owner_user_id == owner, ChannelProfileRecord.chat_id == int(chat_id))).first()
            if record is None:
                record = ChannelProfileRecord(owner_user_id=owner, chat_id=int(chat_id), created_at=now, updated_at=now)
                session.add(record)
            record.label = label.strip()[:255]
            record.timezone_name = timezone_name.strip() or "UTC"
            record.signature = signature.strip()[:2000]
            record.updated_at = now
            session.flush()
            return self._channel_dict(record)

    def list_channel_profiles(self, user_id: int) -> list[dict[str, Any]]:
        with self.Session() as session:
            records = session.scalars(select(ChannelProfileRecord).where(ChannelProfileRecord.owner_user_id == int(user_id)).order_by(ChannelProfileRecord.chat_id)).all()
            return [self._channel_dict(record) for record in records]

    def get_channel_profile(self, user_id: int, chat_id: int) -> dict[str, Any] | None:
        with self.Session() as session:
            record = session.scalars(select(ChannelProfileRecord).where(ChannelProfileRecord.owner_user_id == int(user_id), ChannelProfileRecord.chat_id == int(chat_id))).first()
            return self._channel_dict(record) if record else None

    def remove_channel_profile(self, user_id: int, chat_id: int) -> None:
        with self.Session.begin() as session:
            record = session.scalars(select(ChannelProfileRecord).where(ChannelProfileRecord.owner_user_id == int(user_id), ChannelProfileRecord.chat_id == int(chat_id))).first()
            if record is None:
                raise ProviderPoolError("Channel profile was not found")
            session.delete(record)

    @staticmethod
    def _template_dict(record: TemplateRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_user_id": record.owner_user_id,
            "name": record.name,
            "body": record.body,
            "category": record.category,
            "cta": record.cta,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def upsert_template(self, user_id: int, name: str, body: str, category: str = "", cta: str = "") -> dict[str, Any]:
        if not name.strip() or not body.strip():
            raise ProviderPoolError("Template name and body cannot be empty")
        owner = int(user_id)
        now = self._now()
        with self.Session.begin() as session:
            record = session.scalars(select(TemplateRecord).where(TemplateRecord.owner_user_id == owner, TemplateRecord.name == name.strip())).first()
            if record is None:
                record = TemplateRecord(owner_user_id=owner, name=name.strip()[:80], body=body.strip(), created_at=now, updated_at=now)
                session.add(record)
            record.body = body.strip()
            record.category = category.strip()[:100]
            record.cta = cta.strip()
            record.updated_at = now
            session.flush()
            return self._template_dict(record)

    def list_templates(self, user_id: int) -> list[dict[str, Any]]:
        with self.Session() as session:
            records = session.scalars(select(TemplateRecord).where(TemplateRecord.owner_user_id == int(user_id)).order_by(TemplateRecord.name)).all()
            return [self._template_dict(record) for record in records]

    def get_template(self, user_id: int, name: str) -> dict[str, Any] | None:
        with self.Session() as session:
            record = session.scalars(select(TemplateRecord).where(TemplateRecord.owner_user_id == int(user_id), TemplateRecord.name == name)).first()
            return self._template_dict(record) if record else None

    def remove_template(self, user_id: int, name: str) -> None:
        with self.Session.begin() as session:
            record = session.scalars(select(TemplateRecord).where(TemplateRecord.owner_user_id == int(user_id), TemplateRecord.name == name)).first()
            if record is None:
                raise ProviderPoolError("Template was not found")
            session.delete(record)

    def record_audit(self, user_id: int, action: str, status: str, draft_id: int | None = None, target_chat_id: int | None = None, batch_id: str = "", attempt: int = 1, detail: str = "") -> dict[str, Any]:
        record = AuditLogRecord(owner_user_id=int(user_id), action=action[:64], status=status[:24], draft_id=draft_id, target_chat_id=target_chat_id, batch_id=batch_id[:80], attempt=int(attempt), detail=detail[:2000], created_at=self._now())
        with self.Session.begin() as session:
            session.add(record)
            session.flush()
            return {
                "id": record.id,
                "owner_user_id": record.owner_user_id,
                "action": record.action,
                "status": record.status,
                "draft_id": record.draft_id,
                "target_chat_id": record.target_chat_id,
                "batch_id": record.batch_id,
                "attempt": record.attempt,
                "detail": record.detail,
                "created_at": record.created_at,
            }

    def list_audit_logs(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self.Session() as session:
            records = session.scalars(select(AuditLogRecord).where(AuditLogRecord.owner_user_id == int(user_id)).order_by(AuditLogRecord.created_at.desc()).limit(limit)).all()
            return [
                {
                    "id": record.id,
                    "action": record.action,
                    "status": record.status,
                    "draft_id": record.draft_id,
                    "target_chat_id": record.target_chat_id,
                    "batch_id": record.batch_id,
                    "attempt": record.attempt,
                    "detail": record.detail,
                    "created_at": record.created_at,
                }
                for record in records
            ]

    @staticmethod
    def _recurring_dict(record: RecurringPostRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_user_id": record.owner_user_id,
            "draft_id": record.draft_id,
            "channel_chat_id": record.channel_chat_id,
            "interval_minutes": record.interval_minutes,
            "next_run_at": record.next_run_at,
            "until_at": record.until_at,
            "active": record.active,
            "last_run_at": record.last_run_at,
            "last_error": record.last_error,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def create_recurring(self, user_id: int, draft_id: int, channel_chat_id: int, interval_minutes: int, next_run_at: datetime, until_at: datetime | None = None) -> dict[str, Any]:
        if interval_minutes < 60:
            raise ProviderPoolError("Recurring interval must be at least 60 minutes")
        now = self._now()
        record = RecurringPostRecord(owner_user_id=int(user_id), draft_id=int(draft_id), channel_chat_id=int(channel_chat_id), interval_minutes=int(interval_minutes), next_run_at=next_run_at, until_at=until_at, active=True, created_at=now, updated_at=now)
        with self.Session.begin() as session:
            session.add(record)
            session.flush()
            return self._recurring_dict(record)

    def list_recurring(self, user_id: int, active_only: bool = False) -> list[dict[str, Any]]:
        with self.Session() as session:
            query = select(RecurringPostRecord).where(RecurringPostRecord.owner_user_id == int(user_id)).order_by(RecurringPostRecord.next_run_at)
            if active_only:
                query = query.where(RecurringPostRecord.active.is_(True))
            return [self._recurring_dict(record) for record in session.scalars(query).all()]

    def claim_due_recurring(self, now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
        current = now or self._now()
        with self.Session.begin() as session:
            query = select(RecurringPostRecord).where(RecurringPostRecord.active.is_(True), RecurringPostRecord.next_run_at <= current).order_by(RecurringPostRecord.next_run_at).limit(limit)
            records = session.scalars(query).all()
            for record in records:
                record.active = False
                record.updated_at = current
            return [self._recurring_dict(record) for record in records]

    def complete_recurring(self, recurring_id: int, next_run_at: datetime | None, last_run_at: datetime, last_error: str = "") -> dict[str, Any]:
        with self.Session.begin() as session:
            record = session.get(RecurringPostRecord, int(recurring_id))
            if record is None:
                raise ProviderPoolError("Recurring post was not found")
            record.last_run_at = last_run_at
            record.last_error = last_error[:1000]
            record.next_run_at = next_run_at or record.next_run_at
            record.active = next_run_at is not None
            record.updated_at = self._now()
            return self._recurring_dict(record)

    def remove_recurring(self, user_id: int, recurring_id: int) -> None:
        with self.Session.begin() as session:
            record = session.scalars(select(RecurringPostRecord).where(RecurringPostRecord.id == int(recurring_id), RecurringPostRecord.owner_user_id == int(user_id))).first()
            if record is None:
                raise ProviderPoolError("Recurring post was not found")
            session.delete(record)

    @staticmethod
    def _draft_dict(record: DraftRecord) -> dict[str, Any]:
        try:
            source_facts = json.loads(record.source_facts_json or "[]")
        except json.JSONDecodeError:
            source_facts = []
        try:
            media_file_ids = json.loads(record.media_file_ids_json or "[]")
        except json.JSONDecodeError:
            media_file_ids = []
        try:
            buttons = json.loads(record.buttons_json or "[]")
        except json.JSONDecodeError:
            buttons = []
        return {
            "id": record.id,
            "owner_user_id": record.owner_user_id,
            "source_text": record.source_text,
            "post_text": record.post_text,
            "category": record.category,
            "cta": record.cta,
            "source_facts": source_facts,
            "needs_review": record.needs_review,
            "status": record.status,
            "channel_chat_id": record.channel_chat_id,
            "scheduled_at": record.scheduled_at,
            "published_at": record.published_at,
            "published_message_id": record.published_message_id,
            "last_error": record.last_error,
            "media_type": record.media_type,
            "media_file_ids": media_file_ids,
            "buttons": buttons,
            "link_preview_disabled": record.link_preview_disabled,
            "watermark_text": record.watermark_text,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def create_draft(self, user_id: int, source_text: str, result: dict[str, Any], media: dict[str, Any] | None = None) -> dict[str, Any]:
        post_text = str(result.get("post", "")).strip()
        if not post_text:
            raise ProviderPoolError("A draft cannot be created without post text")
        now = self._now()
        media = media or {}
        record = DraftRecord(
            owner_user_id=int(user_id),
            source_text=source_text,
            post_text=post_text,
            category=str(result.get("category", "")),
            cta=str(result.get("cta", "")),
            source_facts_json=json.dumps(result.get("source_facts", []), ensure_ascii=False),
            needs_review=bool(result.get("needs_review", False)),
            media_type=str(media.get("media_type", "text")),
            media_file_ids_json=json.dumps([str(value) for value in media.get("media_file_ids", [])], ensure_ascii=False),
            buttons_json=json.dumps(media.get("buttons", []), ensure_ascii=False),
            link_preview_disabled=bool(media.get("link_preview_disabled", False)),
            watermark_text=str(media.get("watermark_text", ""))[:500],
            status="draft",
            created_at=now,
            updated_at=now,
        )
        with self.Session.begin() as session:
            session.add(record)
            session.flush()
            return self._draft_dict(record)

    def update_draft_media(self, user_id: int, draft_id: int, media_type: str | None = None, media_file_ids: list[str] | None = None, buttons: list[dict[str, str]] | None = None, link_preview_disabled: bool | None = None, watermark_text: str | None = None) -> dict[str, Any]:
        with self.Session.begin() as session:
            record = session.scalar(select(DraftRecord).where(DraftRecord.id == int(draft_id), DraftRecord.owner_user_id == int(user_id)))
            if record is None:
                raise ProviderPoolError("Draft was not found")
            if media_type is not None:
                record.media_type = media_type
            if media_file_ids is not None:
                record.media_file_ids_json = json.dumps([str(value) for value in media_file_ids], ensure_ascii=False)
            if buttons is not None:
                record.buttons_json = json.dumps(buttons, ensure_ascii=False)
            if link_preview_disabled is not None:
                record.link_preview_disabled = bool(link_preview_disabled)
            if watermark_text is not None:
                record.watermark_text = watermark_text[:500]
            record.updated_at = self._now()
            return self._draft_dict(record)

    def get_draft(self, user_id: int, draft_id: int) -> dict[str, Any] | None:
        with self.Session() as session:
            record = session.scalar(
                select(DraftRecord).where(
                    DraftRecord.id == int(draft_id),
                    DraftRecord.owner_user_id == int(user_id),
                )
            )
            return self._draft_dict(record) if record else None

    def list_drafts(self, user_id: int, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.Session() as session:
            query = select(DraftRecord).where(DraftRecord.owner_user_id == int(user_id)).order_by(DraftRecord.updated_at.desc()).limit(limit)
            if status:
                query = query.where(DraftRecord.status == status)
            return [self._draft_dict(record) for record in session.scalars(query).all()]

    def update_draft(self, user_id: int, draft_id: int, **changes: Any) -> dict[str, Any]:
        allowed = {"post_text", "status", "channel_chat_id", "scheduled_at", "last_error", "published_at", "published_message_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ProviderPoolError(f"Unsupported draft fields: {', '.join(sorted(unknown))}")
        with self.Session.begin() as session:
            record = session.scalar(
                select(DraftRecord).where(
                    DraftRecord.id == int(draft_id),
                    DraftRecord.owner_user_id == int(user_id),
                )
            )
            if record is None:
                raise ProviderPoolError(f"Draft {draft_id} was not found")
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = self._now()
            return self._draft_dict(record)

    def claim_due_drafts(self, now: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
        current = now or self._now()
        with self.Session.begin() as session:
            query = (
                select(DraftRecord)
                .where(
                    DraftRecord.status == "scheduled",
                    DraftRecord.scheduled_at.is_not(None),
                    DraftRecord.scheduled_at <= current,
                )
                .order_by(DraftRecord.scheduled_at)
                .limit(limit)
            )
            records = session.scalars(query).all()
            for record in records:
                record.status = "publishing"
                record.updated_at = current
            return [self._draft_dict(record) for record in records]

    def get_draft_by_id(self, draft_id: int) -> dict[str, Any] | None:
        with self.Session() as session:
            record = session.get(DraftRecord, int(draft_id))
            return self._draft_dict(record) if record else None

    def get_preferences(self, user_id: int) -> dict[str, str]:
        owner = int(user_id)
        with self.Session.begin() as session:
            record = session.get(UserPreferenceRecord, owner)
            if record is None:
                record = UserPreferenceRecord(user_id=owner, language="English", default_niche="", style="professional", updated_at=self._now())
                session.add(record)
            return {
                "language": record.language,
                "default_niche": record.default_niche,
                "style": record.style,
            }

    def get_preference(self, user_id: int, key: str, default: str = "") -> str:
        return self.get_preferences(user_id).get(key, default)

    def set_preference(self, user_id: int, key: str, value: str) -> None:
        if key not in ALLOWED_PREFERENCE_KEYS:
            raise ProviderPoolError(f"Supported preferences: {', '.join(sorted(ALLOWED_PREFERENCE_KEYS))}")
        value = value.strip()
        if key == "language" and value.lower() != "english":
            raise ProviderPoolError("This English-language build supports only language=English")
        if len(value) > 255:
            raise ProviderPoolError("Preference value is too long")
        owner = int(user_id)
        with self.Session.begin() as session:
            record = session.get(UserPreferenceRecord, owner)
            if record is None:
                record = UserPreferenceRecord(user_id=owner, language="English", default_niche="", style="professional", updated_at=self._now())
                session.add(record)
            setattr(record, key, value)
            record.updated_at = self._now()
