"""Encrypted provider profile storage for the Telegram bot.

API keys are encrypted at rest with a Fernet key supplied through
PROVIDER_STORE_KEY. The encryption key is never stored in this repository or in
the encrypted profile file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
ALLOWED_RESPONSE_FORMATS = {"auto", "json_schema", "json_object", "none"}
ALLOWED_TOKEN_PARAMS = {"auto", "max_tokens", "max_completion_tokens"}
ALLOWED_REASONING = {"", "minimal", "low", "medium", "high"}


class ProviderPoolError(RuntimeError):
    """Raised when the encrypted provider pool cannot be read or written safely."""


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


class ProviderPoolStore:
    def __init__(self, path: str | Path, encryption_key: str) -> None:
        if not encryption_key:
            raise ProviderPoolError("PROVIDER_STORE_KEY is required for provider-pool storage")
        try:
            self.fernet = Fernet(encryption_key.encode())
        except Exception as exc:
            raise ProviderPoolError(
                "PROVIDER_STORE_KEY must be a valid Fernet key; generate one with Fernet.generate_key()."
            ) from exc
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = {"active": "", "providers": {}}
        self._load()

    @classmethod
    def from_environment(cls) -> "ProviderPoolStore | None":
        key = os.getenv("PROVIDER_STORE_KEY", "").strip()
        if not key:
            # The bot token is already required and secret. Derive a stable Fernet
            # key so admins can manage providers in Telegram without another secret.
            bot_token = os.getenv("BOT_TOKEN", "").strip()
            if not bot_token:
                return None
            digest = hashlib.sha256(
                ("telegram-provider-pool:" + bot_token).encode("utf-8")
            ).digest()
            key = base64.urlsafe_b64encode(digest).decode("ascii")
        path = os.getenv("PROVIDER_POOL_PATH", "provider_pool.enc")
        return cls(path, key)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            encrypted = self.path.read_bytes()
            raw = json.loads(self.fernet.decrypt(encrypted).decode("utf-8"))
        except (OSError, InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderPoolError(
                "Provider pool could not be decrypted. Check PROVIDER_STORE_KEY and file integrity."
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict):
            raise ProviderPoolError("Provider pool has an invalid structure")
        self._state = {"active": str(raw.get("active", "")), "providers": raw["providers"]}

    def _save(self) -> None:
        payload = json.dumps(self._state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        encrypted = self.fernet.encrypt(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".provider_pool.", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

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
            query = [(key, "[redacted]" if key.lower() in sensitive else val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)]
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        except Exception:
            return "[redacted endpoint]"

    def list_profiles(self) -> list[dict[str, Any]]:
        active = self.active_name
        result = []
        for name, raw in sorted(self._state["providers"].items()):
            profile = ProviderProfile.from_dict({"name": name, **raw})
            result.append(
                {
                    "name": profile.name,
                    "active": profile.name == active,
                    "api_key": self.mask_key(profile.api_key),
                    "base_url": self.redact_url(profile.base_url) or "provider default",
                    "model": profile.model,
                    "response_format": profile.response_format,
                    "max_tokens_param": profile.max_tokens_param,
                }
            )
        return result

    @property
    def active_name(self) -> str:
        active = str(self._state.get("active", ""))
        return active if active in self._state["providers"] else ""

    def get(self, name: str | None = None) -> ProviderProfile | None:
        selected = name or self.active_name
        if not selected or selected not in self._state["providers"]:
            return None
        profile = ProviderProfile.from_dict({"name": selected, **self._state["providers"][selected]})
        profile.validate()
        return profile

    def upsert(self, profile: ProviderProfile) -> None:
        profile.validate()
        self._state["providers"][profile.name] = asdict(profile)
        if not self._state.get("active"):
            self._state["active"] = profile.name
        self._save()

    def activate(self, name: str) -> ProviderProfile:
        profile = self.get(name)
        if profile is None:
            raise ProviderPoolError(f"Provider '{name}' was not found")
        self._state["active"] = profile.name
        self._save()
        return profile

    def remove(self, name: str) -> None:
        if name not in self._state["providers"]:
            raise ProviderPoolError(f"Provider '{name}' was not found")
        del self._state["providers"][name]
        if self._state.get("active") == name:
            self._state["active"] = next(iter(self._state["providers"]), "")
        self._save()
