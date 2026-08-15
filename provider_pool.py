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
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, select
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
