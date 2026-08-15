"""Telegram AI Content Strategist and Growth Manager.

The bot intentionally contains only content strategy workflows. It does not scan
credentials, access tokens, promo codes, or private account data.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Any
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from openai import OpenAI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from groupscan import (
    GroupScanInputError,
    MAX_FILE_BYTES as GROUPSCAN_MAX_FILE_BYTES,
    parse_groups as parse_group_records,
    parse_member_count,
    render_report,
    split_niche_and_groups,
)
from provider_pool import ProviderPoolError, ProviderPoolStore, ProviderProfile


load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger("telegram_content_agent")


SYSTEM_IDENTITY = """
You are an expert Telegram Content Strategist and Growth Manager.
Your objective is to create engaging content, logically distribute it, and identify
high-value target groups for audience growth.

Core responsibilities:
1. Transform only the provided raw information, article, link, or message into
   an engaging Telegram post.
2. Every post must have a strong hook/headline, short mobile-friendly sections,
   appropriate emojis used sparingly, and a clear call-to-action.
3. For curation or forwarding, write a compelling 1-2 sentence English intro that
   adds context. Never forward content blindly.
4. Evaluate Telegram groups only from their supplied name, description, and member
   count. Strictly flag spam, irrelevant niches, and groups whose supplied evidence
   indicates low quality.

Non-negotiable rules:
- Output English by default unless the user explicitly requests another language.
- Never invent facts, news, statistics, engagement, or group characteristics.
- If the input does not support a claim, say that it is unknown or needs review.
- Use a professional, authoritative, approachable tone; avoid spammy language.
- Keep paragraphs short and readable on a phone.
""".strip()


POST_SCHEMA = {
    "type": "object",
    "properties": {
        "post": {"type": "string"},
        "category": {"type": "string"},
        "cta": {"type": "string"},
        "source_facts": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "boolean"},
    },
    "required": ["post", "category", "cta", "source_facts", "needs_review"],
    "additionalProperties": False,
}


CURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "should_forward": {"type": "boolean"},
        "category": {"type": "string"},
        "intro": {"type": "string"},
        "reason": {"type": "string"},
        "needs_review": {"type": "boolean"},
    },
    "required": [
        "should_forward",
        "category",
        "intro",
        "reason",
        "needs_review",
    ],
    "additionalProperties": False,
}


AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "mode": {"type": "string"},
        "source_facts": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": "boolean"},
    },
    "required": ["answer", "mode", "source_facts", "needs_review"],
    "additionalProperties": False,
}


SCOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "fit_score": {"type": "number"},
                    "match": {"type": "boolean"},
                    "spam_flag": {"type": "boolean"},
                    "quality_label": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "unknown"],
                    },
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "action": {
                        "type": "string",
                        "enum": ["target", "review", "exclude"],
                    },
                },
                "required": [
                    "name",
                    "fit_score",
                    "match",
                    "spam_flag",
                    "quality_label",
                    "reason",
                    "evidence",
                    "action",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}


def _csv_ints(value: str | None) -> set[int]:
    result: set[int] = set()
    for item in (value or "").split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError:
                logger.warning("Ignoring invalid numeric ID in configuration: %s", item)
    return result


def normalize_base_url(value: str | None) -> str:
    """Normalize provider URLs while preserving OpenAI-compatible `/v1` paths."""
    base = (value or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/chat/completions", "/responses", "/models"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def _load_targets(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        logger.error("TARGETS_JSON is invalid JSON: %s", exc)
        return []
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        logger.error("TARGETS_JSON must be a JSON object or array")
        return []

    targets: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict) or "chat_id" not in item:
            continue
        try:
            chat_id = int(item["chat_id"])
        except (TypeError, ValueError):
            continue
        targets.append(
            {
                "chat_id": chat_id,
                "label": str(item.get("label", str(chat_id))),
                "description": str(item.get("description", "")),
                "allowed_categories": [
                    str(x) for x in item.get("allowed_categories", [])
                ],
            }
        )
    return targets


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")).strip()
LLM_BASE_URL = normalize_base_url(os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_API_BASE", ""))
LLM_MODEL = (os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5-mini").strip()
LLM_TIMEOUT_SECONDS = max(10.0, float(os.getenv("LLM_TIMEOUT_SECONDS", "60")))
LLM_MAX_RETRIES = max(0, int(os.getenv("LLM_MAX_RETRIES", "2")))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
ADMIN_IDS = _csv_ints(os.getenv("ADMIN_IDS"))
GROUPSCAN_ALLOWED_CHAT_IDS = _csv_ints(os.getenv("GROUPSCAN_ALLOWED_CHAT_IDS"))
TARGETS = _load_targets(os.getenv("TARGETS_JSON"))
MAX_INPUT_CHARS = max(1000, int(os.getenv("MAX_INPUT_CHARS", "8000")))
PUBLISH_MAX_RETRIES = max(0, min(5, int(os.getenv("PUBLISH_MAX_RETRIES", "2"))))
PUBLISH_RETRY_DELAY_SECONDS = max(1.0, min(120.0, float(os.getenv("PUBLISH_RETRY_DELAY_SECONDS", "5"))))
MAX_MEDIA_FILES = max(1, min(10, int(os.getenv("MAX_MEDIA_FILES", "10"))))
MAX_BUTTONS = max(1, min(20, int(os.getenv("MAX_BUTTONS", "20"))))
PROVIDER_POOL: ProviderPoolStore | None = None
PROVIDER_POOL_ERROR = ""
try:
    PROVIDER_POOL = ProviderPoolStore.from_environment()
except ProviderPoolError as exc:
    PROVIDER_POOL_ERROR = str(exc)
    logger.error("Database provider store disabled: %s", exc)


class AgentError(RuntimeError):
    """Raised when the model cannot provide a safe structured answer."""


class ContentAgent:
    def __init__(self, provider: ProviderProfile | None = None, user_id: int | None = None) -> None:
        selected = provider
        self.user_id = int(user_id) if user_id is not None else None
        self.preferences = {"language": "English", "default_niche": "", "style": "professional"}
        if PROVIDER_POOL is not None and self.user_id is not None:
            try:
                selected = selected or PROVIDER_POOL.get(self.user_id)
                self.preferences = PROVIDER_POOL.get_preferences(self.user_id)
            except ProviderPoolError as exc:
                logger.warning("User provider or preferences could not be loaded; using environment fallback: %s", exc)
        elif selected is None and PROVIDER_POOL is not None:
            logger.warning("No Telegram user ID was supplied; user-scoped provider selection is unavailable")
        self.provider_name = selected.name if selected else "environment"
        self.api_key = selected.api_key if selected else LLM_API_KEY
        self.base_url = normalize_base_url(selected.base_url) if selected else LLM_BASE_URL
        self.model = selected.model if selected else LLM_MODEL
        self.response_format = selected.response_format if selected else (os.getenv("LLM_RESPONSE_FORMAT", "auto") or "auto").strip().lower()
        self.max_tokens_param = selected.max_tokens_param if selected else (os.getenv("LLM_MAX_TOKENS_PARAM", "auto") or "auto").strip().lower()
        self.timeout_seconds = selected.timeout_seconds if selected else LLM_TIMEOUT_SECONDS
        self.max_retries = selected.max_retries if selected else LLM_MAX_RETRIES
        self.reasoning_effort = selected.reasoning_effort if selected else LLM_REASONING_EFFORT
        self.language = self.preferences.get("language", "English")
        self.default_niche = self.preferences.get("default_niche", "")
        self.style = self.preferences.get("style", "professional")
        if not self.api_key:
            raise AgentError("No API key is configured. Add one with /provider_add or set LLM_API_KEY.")
        client_kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout_seconds,
            "max_retries": self.max_retries,
        }
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        self.client = OpenAI(**client_kwargs)

    def _response_modes(self) -> list[str]:
        configured = self.response_format
        if configured in {"json_schema", "json_object", "none"}:
            return [configured]
        # Many OpenAI-compatible providers implement JSON mode but not strict JSON
        # schema. Try the strongest contract first, then degrade safely.
        return ["json_schema", "json_object", "none"]

    def _token_parameter(self, max_tokens: int) -> dict[str, int]:
        configured = self.max_tokens_param
        if configured == "max_tokens":
            return {"max_tokens": max_tokens}
        if configured == "max_completion_tokens":
            return {"max_completion_tokens": max_tokens}
        model = self.model.lower()
        model_name = model.rsplit("/", 1)[-1]
        if model_name.startswith(("gpt-5", "o1", "o3")):
            return {"max_completion_tokens": max_tokens}
        return {"max_tokens": max_tokens}

    @staticmethod
    def _json_text(content: Any) -> str:
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            ).strip()
        else:
            text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        if not text.startswith(("{", "[")):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        return text

    async def _structured(
        self, *, task: str, schema_name: str, schema: dict[str, Any], max_tokens: int = 2200
    ) -> dict[str, Any]:
        schema_text = json.dumps(schema, ensure_ascii=False)
        user_task = (
            task[:MAX_INPUT_CHARS]
            + "\n\nReturn only one valid JSON object. It must follow this schema exactly:\n"
            + schema_text
        )
        last_error: Exception | None = None
        for response_mode in self._response_modes():
            request: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            SYSTEM_IDENTITY
                            + "\n\nUSER PREFERENCES (apply when relevant; they do not override safety rules): "
                            + json.dumps(self.preferences, ensure_ascii=False)
                        ),
                    },
                    {"role": "user", "content": user_task},
                ],
                **self._token_parameter(max_tokens),
            }
            if response_mode == "json_schema":
                request["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                }
            elif response_mode == "json_object":
                request["response_format"] = {"type": "json_object"}
            if self.reasoning_effort and self.model.lower().rsplit("/", 1)[-1].startswith("gpt-5"):
                request["extra_body"] = {"reasoning": {"effort": self.reasoning_effort}}

            try:
                response = await asyncio.to_thread(
                    self.client.chat.completions.create, **request
                )
                if not response.choices:
                    raise AgentError("The language model returned no choices")
                message = response.choices[0].message
                if getattr(message, "refusal", None):
                    raise AgentError("The language model refused the request")
                text = self._json_text(getattr(message, "content", None))
                if not text:
                    raise AgentError("The language model returned an empty response")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise AgentError("The language model returned an unexpected shape")
                return data
            except AgentError as exc:
                last_error = exc
                logger.warning("Structured response mode %s failed: %s", response_mode, exc)
            except Exception as exc:
                last_error = exc
                logger.warning("LLM request mode %s failed; trying fallback: %s", response_mode, exc)

        logger.exception("All LLM response modes failed", exc_info=last_error)
        raise AgentError(
            "The language model request failed. Check LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, "
            "and LLM_RESPONSE_FORMAT."
        ) from last_error

    async def agent(self, prompt: str) -> dict[str, Any]:
        return await self._structured(
            schema_name="telegram_agent_answer",
            schema=AGENT_SCHEMA,
            max_tokens=2600,
            task=f"""
Respond to the user's Telegram strategy request as the configured Content Strategist
and Growth Manager. Default to English. Choose the most useful mode, such as
content_creation, curation, campaign_planning, audience_growth, or group_scouting.
Use only facts supplied in the request. If the user asks for unsupported news,
statistics, engagement, or audience claims, explain the limitation and set
needs_review to true. Keep the answer practical, mobile-friendly, and professional.

USER REQUEST:
{prompt}
""",
        )

    async def create_post(self, source: str, language: str = "English") -> dict[str, Any]:
        return await self._structured(
            schema_name="telegram_post",
            schema=POST_SCHEMA,
            task=f"""
Create one ready-to-publish Telegram post from the source below.
Output language: {language}. Use only source-supported facts.
The post itself must include: a strong hook, short sections or bullets, restrained
and relevant emojis, and a clear CTA. Do not mention these instructions.
If the source is too vague to support a factual claim, keep the wording general and
set needs_review to true. Preserve any source link exactly.

SOURCE:
{source}
""",
        )

    async def curate(self, source: str, target: dict[str, Any]) -> dict[str, Any]:
        allowed = ", ".join(target.get("allowed_categories", [])) or "not specified"
        return await self._structured(
            schema_name="telegram_curation",
            schema=CURATION_SCHEMA,
            task=f"""
Evaluate whether the supplied content is strictly relevant to this target Telegram
channel/group. Use only the supplied content and target description.
Target label: {target.get('label', '')}
Target description: {target.get('description', '')}
Allowed categories: {allowed}

If relevant, write a compelling 1-2 sentence English intro that adds context before
forwarding. Do not repeat unsupported facts. If relevance is uncertain, set
should_forward to false and needs_review to true. Do not make the intro clickbait.

CONTENT TO CURATE:
{source}
""",
        )

    async def scout(self, groups: list[dict[str, Any]], niche: str) -> dict[str, Any]:
        serialized = json.dumps(groups, ensure_ascii=False)
        result = await self._structured(
            schema_name="telegram_group_scout",
            schema=SCOUT_SCHEMA,
            max_tokens=3500,
            task=f"""
Evaluate the following Telegram groups for the target niche: {niche or 'infer only from the supplied context'}.
For every group, return exactly one result and preserve its name.
Use only each supplied name, description, and member count. Do not claim real
engagement, activity, or audience quality unless the input explicitly says so.
Strictly flag obvious spam, irrelevant niches, and low-quality signals present in the
input. If quality cannot be established from the input, use quality_label=unknown and
action=review rather than guessing.
fit_score must be a 0-100 estimate based only on niche relevance, not popularity.

GROUPS:
{serialized}
""",
        )
        expected = {group["name"].casefold() for group in groups}
        actual = {str(item.get("name", "")).casefold() for item in result.get("groups", [])}
        if actual != expected:
            raise AgentError("GroupScan result did not contain one matching result per supplied group")
        return result


# Re-export the safe parser name for compatibility with earlier imports.
parse_groups = parse_group_records


def group_scan_allowed(update: Update) -> bool:
    """Optionally restrict GroupScan commands to explicitly configured chats."""
    if not GROUPSCAN_ALLOWED_CHAT_IDS:
        return True
    chat = update.effective_chat
    return bool(chat and chat.id in GROUPSCAN_ALLOWED_CHAT_IDS)


def source_from_message(message: Any) -> str:
    if message is None:
        return ""
    text = message.text or message.caption or ""
    return text.strip()


def command_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    args = " ".join(context.args).strip()
    if args:
        return args
    if update.message and update.message.reply_to_message:
        return source_from_message(update.message.reply_to_message)
    return ""


def full_command_payload(update: Update) -> str:
    """Preserve newlines after a command; context.args intentionally tokenizes them."""
    text = (update.message.text if update.message else "") or ""
    return text.partition(" ")[2].strip()


async def group_scan_payload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    reply = update.message.reply_to_message if update.message else None
    if reply and reply.document:
        file_size = reply.document.file_size or 0
        if file_size > GROUPSCAN_MAX_FILE_BYTES:
            raise GroupScanInputError("Attached group list is larger than the configured input limit")
        telegram_file = await context.bot.get_file(reply.document.file_id)
        data = await telegram_file.download_as_bytearray()
        try:
            document_text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise GroupScanInputError("Attached group list must be UTF-8 text") from exc
        niche = " ".join(context.args).strip()
        return f"{niche}\n{document_text}".strip() if niche else document_text.strip()
    payload = full_command_payload(update)
    reply_text = source_from_message(reply) if reply else ""
    if reply_text:
        return f"{payload}\n{reply_text}".strip() if payload else reply_text
    return payload


def parse_buttons(value: str) -> list[dict[str, str]]:
    buttons: list[dict[str, str]] = []
    for raw in value.split("||"):
        raw = raw.strip()
        if not raw:
            continue
        if "|" not in raw:
            raise ValueError("Each button must use Label | URL format")
        label, url = [part.strip() for part in raw.split("|", 1)]
        if not label or not url.startswith(("https://", "http://", "tg://")):
            raise ValueError("Buttons require a label and an http(s) or tg:// URL")
        buttons.append({"label": label[:64], "url": url[:2048]})
    if len(buttons) > MAX_BUTTONS:
        raise ValueError(f"A maximum of {MAX_BUTTONS} buttons is allowed")
    return buttons


def keyboard_from_buttons(buttons: list[dict[str, str]]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(item["label"], url=item["url"])] for item in buttons])


def add_watermark(image_bytes: bytes, watermark_text: str) -> bytes:
    if not watermark_text.strip():
        return image_bytes
    try:
        from PIL import Image, ImageDraw
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(image)
        margin = max(8, image.width // 50)
        draw.text((margin, image.height - margin - 18), watermark_text[:120], fill=(255, 255, 255, 180))
        output = io.BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=90)
        return output.getvalue()
    except Exception as exc:
        raise ProviderPoolError(f"Watermarking failed: {exc}") from exc


def chunk_text(text: str, size: int = 3900) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def target_for(chat_id: int) -> dict[str, Any] | None:
    return next((item for item in TARGETS if item["chat_id"] == chat_id), None)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_IDS and user.id in ADMIN_IDS)


def provider_store_or_error() -> ProviderPoolStore | None:
    if PROVIDER_POOL is None:
        return None
    return PROVIDER_POOL


def telegram_user_id(update: Update) -> int | None:
    user = update.effective_user
    return int(user.id) if user else None


def provider_list_text(user_id: int | None = None) -> str:
    if PROVIDER_POOL is None:
        detail = PROVIDER_POOL_ERROR or "BOT_TOKEN is not configured."
        return f"The provider pool is unavailable. {detail}"
    if user_id is None:
        return "A Telegram user ID is required to load provider profiles."
    profiles = PROVIDER_POOL.list_profiles(user_id)
    lines = ["🔐 API Provider Pool", ""]
    if not profiles:
        lines.append("No provider profiles exist yet. Use /provider_add to add one.")
    for item in profiles:
        marker = "✅ ACTIVE" if item["active"] else "○"
        lines.append(
            f"{marker} {item['name']} | {item['model']}\n"
            f"  Key: {item['api_key']}\n"
            f"  Endpoint: {item['base_url']}\n"
            f"  Format: {item['response_format']} | Tokens: {item['max_tokens_param']}"
        )
    if not profiles and LLM_API_KEY:
        lines.append("\nEnvironment fallback: configured (key hidden)")
    elif profiles:
        lines.append("\nUse /provider_use <name> to change the active profile.")
    return "\n".join(lines)


PROVIDER_NAME, PROVIDER_KEY, PROVIDER_ENDPOINT, PROVIDER_MODEL, PROVIDER_OPTIONS = range(5)


def parse_utc_datetime(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Use ISO-8601 UTC format, for example 2026-08-20T09:00:00Z") from exc
    if parsed.tzinfo is None:
        raise ValueError("The schedule time must include a timezone, for example Z or +00:00")
    return parsed.astimezone(timezone.utc)


def parse_local_datetime(value: str, timezone_name: str = "UTC") -> datetime:
    raw = value.strip()
    if raw.endswith("Z") or "+" in raw[10:] or ("-" in raw[10:] and "T" in raw):
        return parse_utc_datetime(raw)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Use ISO-8601 format, for example 2026-08-20T09:00:00") from exc
    return parsed.replace(tzinfo=zone).astimezone(timezone.utc)


def validate_timezone(value: str) -> str:
    name = value.strip() or "UTC"
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc
    return name


def draft_owner_id(update: Update) -> int | None:
    return telegram_user_id(update)


def draft_store_required(update: Update) -> tuple[ProviderPoolStore, int] | None:
    if PROVIDER_POOL is None or draft_owner_id(update) is None:
        return None
    return PROVIDER_POOL, int(draft_owner_id(update))


def format_draft(draft: dict[str, Any], include_source: bool = False) -> str:
    scheduled = draft.get("scheduled_at")
    if isinstance(scheduled, datetime):
        scheduled_text = scheduled.astimezone(timezone.utc).isoformat()
    else:
        scheduled_text = str(scheduled or "not scheduled")
    lines = [
        f"Draft #{draft['id']} — {draft['status'].upper()}",
        f"Category: {draft.get('category') or 'Unknown'}",
        f"Scheduled: {scheduled_text}",
        "",
        draft["post_text"],
    ]
    if include_source:
        lines.extend(["", "Source:", str(draft.get("source_text") or "")])
    if draft.get("last_error"):
        lines.extend(["", f"Last error: {draft['last_error']}"])
    return "\n".join(lines)


def usage_text() -> str:
    return (
        "Available commands\n\n"
        "/agent <request> — Ask the AI Content Strategist a general request\n"
        "/post <source> — Turn source material into a Telegram post\n"
        "/curate <content> — Classify content and draft a context intro\n"
        "/groupscan <niche>\nGroup Name | description | members — Evaluate groups\n"
        "/scout — Backward-compatible alias for /groupscan\n"
        "/id — Show the current chat ID\n"
        "/forward <target_chat_id> — Curate and forward a replied message\n"
        "/targets — Show allowed forwarding targets (admin)\n"
        "/provider_list — List API provider profiles (admin)\n"
        "/provider_add — Add an API key, endpoint, and model profile (admin)\n"
        "/provider_use <name> — Select the active provider (admin)\n"
        "/provider_test [name] — Test a provider connection (admin)\n"
        "/provider_remove <name> — Remove a provider profile (admin)\n"
        "/channel_add <chat_id> <timezone> <label> || <signature> — Save a channel profile (admin)\n"
        "/channel_list — List channel profiles\n"
        "/channel_remove <chat_id> — Remove a channel profile (admin)\n"
        "/template_add <name> || <body> || <category> || <CTA> — Save a template\n"
        "/template_list — List templates\n"
        "/template_post <name> <source> — Create a template-based draft\n"
        "/template_remove <name> — Remove a template\n"
        "/repeat <draft_id> <chat_id> <minutes> <start> [until] — Create a recurring post (admin)\n"
        "/repeat_list — List recurring posts\n"
        "/repeat_remove <id> — Remove a recurring post (admin)\n"
        "/publish_multi <draft_id> <chat_ids> — Publish to multiple channels (admin)\n"
        "/batch_publish <draft_ids> <chat_id> — Publish multiple drafts (admin)\n"
        "/audit — Show recent publishing audit logs\n"
        "/slideshow_add <draft_id> — Add a photo to a slideshow album\n"
        "/preferences — Show your saved preferences\n"
        "/prefs_set <key> <value> — Save a preference\n"
        "/help — Show this help\n\n"
        "You can put content after a command or reply to a message and use the command."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hello. I am your Telegram Content Strategist & Growth Manager.\n\n"
        "I can turn raw information into mobile-friendly Telegram posts, curate content, "
        "perform smart forwarding, evaluate target groups, and manage API provider profiles.\n\n"
        + usage_text()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(usage_text())


async def provider_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return ConversationHandler.END
    if not update.effective_chat or update.effective_chat.type != "private":
        await update.message.reply_text("For security, API-key entry is available only in the bot's private chat.")
        return ConversationHandler.END
    if provider_store_or_error() is None:
        await update.message.reply_text(
            "The provider pool is unavailable. Check BOT_TOKEN or PROVIDER_STORE_KEY."
        )
        return ConversationHandler.END
    context.user_data["provider_draft"] = {}
    context.user_data["provider_user_id"] = telegram_user_id(update)
    await update.message.reply_text(
        "Enter a provider profile name, for example `openai-main` or `openrouter`.\n\n"
        "Use /cancel to stop setup."
    )
    return PROVIDER_NAME


async def provider_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", name):
        await update.message.reply_text("The name must be 1-40 characters and use only letters, numbers, `_`, `-`, or `.`.")
        return PROVIDER_NAME
    context.user_data["provider_draft"]["name"] = name
    await update.message.reply_text("Enter the API key. The bot will attempt to delete this message immediately after receiving it.")
    return PROVIDER_KEY


async def provider_add_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = (update.message.text or "").strip()
    if not key:
        await update.message.reply_text("The API key cannot be empty.")
        return PROVIDER_KEY
    context.user_data["provider_draft"]["api_key"] = key
    try:
        await update.message.delete()
    except TelegramError:
        context.user_data.pop("provider_draft", None)
        await update.message.reply_text(
            "Setup was stopped because the API-key message could not be deleted. "
            "Confirm that you are using the bot in a private admin chat and try again."
        )
        return ConversationHandler.END
    await update.message.reply_text(
        "Enter the endpoint, for example `https://api.openai.com/v1`.\n"
        "Enter `-` to use the provider default endpoint."
    )
    return PROVIDER_ENDPOINT


async def provider_add_endpoint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    endpoint = (update.message.text or "").strip()
    context.user_data["provider_draft"]["base_url"] = "" if endpoint in {"", "-", "default"} else normalize_base_url(endpoint)
    await update.message.reply_text("Enter the model ID, for example `gpt-5-mini`, `openai/gpt-5-mini`, or `llama-3.1-8b-instruct`.")
    return PROVIDER_MODEL


async def provider_add_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    model = (update.message.text or "").strip()
    if not model:
        await update.message.reply_text("The model ID cannot be empty.")
        return PROVIDER_MODEL
    context.user_data["provider_draft"]["model"] = model
    await update.message.reply_text(
        "Enter advanced options on one line:\n"
        "`response_format | tokens_param | timeout_seconds | max_retries | reasoning`\n\n"
        "Example: `auto | auto | 60 | 2 |`\n"
        "Use that example if you do not need custom options."
    )
    return PROVIDER_OPTIONS


async def provider_add_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = [part.strip() for part in (update.message.text or "").split("|")]
    parts += [""] * (5 - len(parts))
    try:
        profile = ProviderProfile(
            name=context.user_data["provider_draft"]["name"],
            api_key=context.user_data["provider_draft"]["api_key"],
            base_url=context.user_data["provider_draft"]["base_url"],
            model=context.user_data["provider_draft"]["model"],
            response_format=parts[0] or "auto",
            max_tokens_param=parts[1] or "auto",
            timeout_seconds=float(parts[2] or "60"),
            max_retries=int(parts[3] or "2"),
            reasoning_effort=parts[4].lower(),
        )
        profile.validate()
        store = provider_store_or_error()
        user_id = context.user_data.get("provider_user_id")
        if store is None or user_id is None:
            raise ProviderPoolError("Provider database is not available for this user")
        store.upsert(user_id, profile)
    except (KeyError, ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The provider profile could not be saved: {exc}\nUse /cancel and restart with /provider_add.")
        context.user_data.pop("provider_draft", None)
        context.user_data.pop("provider_user_id", None)
        return ConversationHandler.END
    context.user_data.pop("provider_draft", None)
    context.user_data.pop("provider_user_id", None)
    await update.message.reply_text(
        f"✅ Provider `{profile.name}` has been saved.\n"
        "The API key will be masked in listings. Use /provider_use "
        f"{profile.name} to activate it."
    )
    return ConversationHandler.END


async def provider_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("provider_draft", None)
    context.user_data.pop("provider_user_id", None)
    await update.message.reply_text("Provider setup has been cancelled.")
    return ConversationHandler.END


async def preferences_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = telegram_user_id(update)
    if PROVIDER_POOL is None or user_id is None:
        await update.message.reply_text("Persistent preferences are unavailable.")
        return
    try:
        prefs = PROVIDER_POOL.get_preferences(user_id)
    except ProviderPoolError as exc:
        await update.message.reply_text(f"Preferences could not be loaded: {exc}")
        return
    await update.message.reply_text(
        "Saved preferences:\n"
        f"• language: {prefs['language']}\n"
        f"• default_niche: {prefs['default_niche'] or '(not set)'}\n"
        f"• style: {prefs['style']}"
    )


async def preferences_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = telegram_user_id(update)
    if PROVIDER_POOL is None or user_id is None:
        await update.message.reply_text("Persistent preferences are unavailable.")
        return
    payload = full_command_payload(update)
    if " " not in payload:
        await update.message.reply_text("Usage: /prefs_set <language|default_niche|style> <value>")
        return
    key, value = payload.split(" ", 1)
    try:
        PROVIDER_POOL.set_preference(user_id, key.strip(), value.strip())
    except ProviderPoolError as exc:
        await update.message.reply_text(f"Preference could not be saved: {exc}")
        return
    await update.message.reply_text(f"✅ Saved preference `{key.strip()}`.")


async def provider_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    await update.message.reply_text(provider_list_text(telegram_user_id(update)))


async def provider_use_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /provider_use <profile_name>")
        return
    store = provider_store_or_error()
    if store is None:
        await update.message.reply_text(provider_list_text(telegram_user_id(update)))
        return
    try:
        user_id = telegram_user_id(update)
        if user_id is None:
            raise ProviderPoolError("Telegram user ID is unavailable")
        profile = store.activate(user_id, context.args[0].strip())
    except ProviderPoolError as exc:
        await update.message.reply_text(f"The provider could not be selected: {exc}")
        return
    await update.message.reply_text(f"✅ Active provider: `{profile.name}` | model: `{profile.model}`")


async def provider_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /provider_remove <profile_name>")
        return
    store = provider_store_or_error()
    if store is None:
        await update.message.reply_text(provider_list_text(telegram_user_id(update)))
        return
    try:
        user_id = telegram_user_id(update)
        if user_id is None:
            raise ProviderPoolError("Telegram user ID is unavailable")
        store.remove(user_id, context.args[0].strip())
    except ProviderPoolError as exc:
        await update.message.reply_text(f"The provider could not be removed: {exc}")
        return
    await update.message.reply_text(f"✅ Provider `{context.args[0].strip()}` has been removed.\n\n{provider_list_text(telegram_user_id(update))}")


async def provider_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    store = provider_store_or_error()
    try:
        user_id = telegram_user_id(update)
        if store and user_id is not None:
            profile = store.get(user_id, context.args[0].strip()) if context.args else store.get(user_id)
        else:
            profile = None
        if context.args and profile is None:
            raise ProviderPoolError(f"Provider '{context.args[0].strip()}' was not found")
        selected_name = profile.name if profile else "environment fallback"
        await update.message.reply_text(f"Testing `{selected_name}`…")
        result = await ContentAgent(provider=profile, user_id=telegram_user_id(update)).agent(
            "Reply with a short English confirmation that this provider connection is working."
        )
        await update.message.reply_text(
            f"✅ The provider connection is working.\n"
            f"Profile: `{selected_name}`\n"
            f"Response: {str(result.get('answer', 'OK'))[:500]}"
        )
    except (AgentError, ProviderPoolError) as exc:
        await update.message.reply_text(f"❌ The provider connection failed: {exc}")


async def run_agent_reply(update: Update, prompt: str) -> None:
    if not prompt:
        await update.message.reply_text("Enter a request for the AI agent.")
        return
    await update.message.reply_text("The AI agent is preparing a response…")
    try:
        result = await ContentAgent(user_id=telegram_user_id(update)).agent(prompt)
        review = "\n\n⚠️ Note: The source information is incomplete and should be reviewed." if result.get("needs_review") else ""
        answer = str(result.get("answer", "")).strip() or "No answer was returned."
        for part in chunk_text(answer + review):
            await update.message.reply_text(part)
    except AgentError as exc:
        await update.message.reply_text(f"The AI agent could not complete the request: {exc}")


async def agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = full_command_payload(update)
    reply = update.message.reply_to_message if update.message else None
    reply_text = source_from_message(reply)
    if reply_text:
        prompt = f"{prompt}\n\nREFERENCE CONTENT:\n{reply_text}".strip() if prompt else reply_text
    if not prompt:
        await update.message.reply_text("Enter a request, or reply to a message and use /agent.")
        return
    await run_agent_reply(update, prompt)


async def freeform_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private":
        await run_agent_reply(update, source_from_message(update.message))


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source = command_payload(update, context)
    if not source:
        await update.message.reply_text("Enter source text, or reply to a message and use /post.")
        return
    await update.message.reply_text("Preparing the content…")
    try:
        result = await ContentAgent(user_id=telegram_user_id(update)).create_post(source)
        store_info = draft_store_required(update)
        if store_info:
            store, owner_id = store_info
            draft = store.create_draft(owner_id, source, result)
            review = "\nReview the source carefully before approval." if result.get("needs_review") else ""
            await update.message.reply_text(
                f"✅ Draft #{draft['id']} created.\n\n"
                f"{draft['post_text'][:3500]}\n\n"
                f"Use /preview {draft['id']}, /approve {draft['id']}, or /draft_edit {draft['id']} <new text>."
                f"{review}"
            )
        else:
            review = "\n\n⚠️ Note: The source information is incomplete; review it before publishing." if result.get("needs_review") else ""
            await update.message.reply_text(result["post"][:4000] + review)
    except (AgentError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The request could not be completed: {exc}")


async def draft_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info:
        await update.message.reply_text("Draft storage is unavailable. Configure DATABASE_URL and encryption settings.")
        return
    store, owner_id = store_info
    status = context.args[0].lower() if context.args else None
    drafts = store.list_drafts(owner_id, status=status)
    if not drafts:
        await update.message.reply_text("No drafts were found.")
        return
    lines = ["Your drafts:"]
    for draft in drafts:
        lines.append(
            f"#{draft['id']} — {draft['status'].upper()} — {draft.get('category') or 'Unknown'} — "
            f"{draft['post_text'][:120].replace(chr(10), ' ')}"
        )
    await update.message.reply_text("\n".join(lines))


async def draft_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /preview <draft_id>")
        return
    store, owner_id = store_info
    try:
        draft_id = int(context.args[0])
        draft = store.get_draft(owner_id, draft_id)
    except (ValueError, ProviderPoolError):
        draft = None
    if not draft:
        await update.message.reply_text("Draft not found.")
        return
    await update.message.reply_text(format_draft(draft, include_source=True)[:4000])


async def draft_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    payload = full_command_payload(update)
    parts = payload.split(" ", 1)
    if not store_info or len(parts) < 2:
        await update.message.reply_text("Usage: /draft_edit <draft_id> <new post text>")
        return
    store, owner_id = store_info
    try:
        draft = store.update_draft(owner_id, int(parts[0]), post_text=parts[1].strip(), status="draft", last_error="")
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The draft could not be edited: {exc}")
        return
    await update.message.reply_text(f"✅ Draft #{draft['id']} updated and returned to DRAFT status.")


async def draft_approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /approve <draft_id>")
        return
    store, owner_id = store_info
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        if not draft:
            raise ProviderPoolError("Draft not found")
        if draft["needs_review"]:
            raise ProviderPoolError("This draft is marked for source review and cannot be approved yet")
        if draft["status"] not in {"draft", "failed"}:
            raise ProviderPoolError(f"Draft status is {draft['status']}; only DRAFT or FAILED drafts can be approved")
        draft = store.update_draft(owner_id, draft["id"], status="approved", last_error="")
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The draft could not be approved: {exc}")
        return
    await update.message.reply_text(f"✅ Draft #{draft['id']} approved. Use /publish or /schedule next.")


def media_from_message(message: Any) -> tuple[str, str] | None:
    if not message:
        return None
    if getattr(message, "photo", None):
        return "photo", str(message.photo[-1].file_id)
    if getattr(message, "video", None):
        return "video", str(message.video.file_id)
    document = getattr(message, "document", None)
    if document and str(getattr(document, "mime_type", "")).startswith("image/"):
        return "photo", str(document.file_id)
    return None


async def media_attach_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or not context.args or not update.message.reply_to_message:
        await update.message.reply_text("Reply to a photo, video, or image document and use /media_attach <draft_id>.")
        return
    store, owner_id = store_info
    media = media_from_message(update.message.reply_to_message)
    if not media:
        await update.message.reply_text("The replied message does not contain a supported photo or video.")
        return
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        if not draft:
            raise ProviderPoolError("Draft not found")
        file_ids = list(draft.get("media_file_ids", []))
        if media[0] == "video" and file_ids:
            raise ProviderPoolError("A video draft can contain one video only")
        if len(file_ids) >= MAX_MEDIA_FILES:
            raise ProviderPoolError(f"A maximum of {MAX_MEDIA_FILES} media files is allowed")
        file_ids.append(media[1])
        draft = store.update_draft_media(owner_id, draft["id"], media_type=media[0], media_file_ids=file_ids)
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Media could not be attached: {exc}")
        return
    await update.message.reply_text(f"✅ Media attached to draft #{draft['id']} ({len(draft['media_file_ids'])} file(s)).")


async def slideshow_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    replied = update.message.reply_to_message if update.message else None
    media = media_from_message(replied)
    if not media or media[0] != "photo":
        await update.message.reply_text("Reply to a photo or image document and use /slideshow_add <draft_id>.")
        return
    await media_attach_command(update, context)


async def media_clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /media_clear <draft_id>")
        return
    store, owner_id = store_info
    try:
        draft = store.update_draft_media(owner_id, int(context.args[0]), media_type="text", media_file_ids=[])
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Media could not be cleared: {exc}")
        return
    await update.message.reply_text(f"✅ Media cleared from draft #{draft['id']}.")


async def buttons_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    payload = full_command_payload(update)
    parts = payload.split(" ", 1)
    if not store_info or len(parts) < 2:
        await update.message.reply_text("Usage: /buttons_set <draft_id> Label | URL || Label 2 | URL 2")
        return
    store, owner_id = store_info
    try:
        buttons = parse_buttons(parts[1])
        draft = store.update_draft_media(owner_id, int(parts[0]), buttons=buttons)
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Buttons could not be saved: {exc}")
        return
    await update.message.reply_text(f"✅ Saved {len(draft['buttons'])} button(s) for draft #{draft['id']}.")


async def preview_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 2 or context.args[1].lower() not in {"on", "off"}:
        await update.message.reply_text("Usage: /preview_set <draft_id> on|off")
        return
    store, owner_id = store_info
    try:
        draft = store.update_draft_media(owner_id, int(context.args[0]), link_preview_disabled=context.args[1].lower() == "off")
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Link-preview settings could not be saved: {exc}")
        return
    state = "disabled" if draft["link_preview_disabled"] else "enabled"
    await update.message.reply_text(f"✅ Link previews are now {state} for draft #{draft['id']}.")


async def watermark_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    payload = full_command_payload(update)
    parts = payload.split(" ", 1)
    if not store_info or len(parts) < 2:
        await update.message.reply_text("Usage: /watermark_set <draft_id> <watermark text>")
        return
    store, owner_id = store_info
    try:
        draft = store.update_draft_media(owner_id, int(parts[0]), watermark_text=parts[1].strip())
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Watermark settings could not be saved: {exc}")
        return
    await update.message.reply_text(f"✅ Watermark saved for draft #{draft['id']}.")


async def _send_text_with_retries(
    bot: Any,
    store: ProviderPoolStore,
    owner_user_id: int,
    text: str,
    target_chat_id: int,
    action: str,
    draft_id: int | None = None,
    batch_id: str = "",
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, PUBLISH_MAX_RETRIES + 2):
        try:
            store.record_audit(owner_user_id, action, "attempting", draft_id, target_chat_id, batch_id, attempt)
            message = await bot.send_message(chat_id=target_chat_id, text=text)
            store.record_audit(owner_user_id, action, "success", draft_id, target_chat_id, batch_id, attempt, f"message_id={message.message_id}")
            return message
        except TelegramError as exc:
            last_error = exc
            store.record_audit(owner_user_id, action, "failed", draft_id, target_chat_id, batch_id, attempt, str(exc))
            if attempt <= PUBLISH_MAX_RETRIES:
                await asyncio.sleep(PUBLISH_RETRY_DELAY_SECONDS * attempt)
    raise TelegramError(str(last_error or "Telegram delivery failed"))


async def _send_draft_with_retries(bot: Any, store: ProviderPoolStore, draft: dict[str, Any], target_chat_id: int, action: str, batch_id: str = "") -> Any:
    owner_user_id = int(draft["owner_user_id"])
    body = draft["post_text"]
    channel_profile = store.get_channel_profile(owner_user_id, int(target_chat_id))
    if channel_profile and channel_profile.get("signature"):
        body = body.rstrip() + "\n\n" + channel_profile["signature"]
    keyboard = keyboard_from_buttons(draft.get("buttons", []))
    media_ids = list(draft.get("media_file_ids", []))
    media_type = draft.get("media_type", "text")
    if media_type == "video" and draft.get("watermark_text"):
        raise ProviderPoolError("Video watermarking is not supported; use an image draft or remove the watermark")

    async def send_once() -> Any:
        if not media_ids:
            preview = LinkPreviewOptions(is_disabled=bool(draft.get("link_preview_disabled", False)))
            return await bot.send_message(chat_id=int(target_chat_id), text=body, link_preview_options=preview, reply_markup=keyboard)
        if media_type == "video":
            return await bot.send_video(chat_id=int(target_chat_id), video=media_ids[0], caption=body[:1024], reply_markup=keyboard)
        photo_values: list[Any] = []
        for file_id in media_ids[:MAX_MEDIA_FILES]:
            if draft.get("watermark_text"):
                telegram_file = await bot.get_file(file_id)
                raw = bytes(await telegram_file.download_as_bytearray())
                photo_values.append(io.BytesIO(add_watermark(raw, draft["watermark_text"])))
            else:
                photo_values.append(file_id)
        if len(photo_values) == 1:
            return await bot.send_photo(chat_id=int(target_chat_id), photo=photo_values[0], caption=body[:1024], reply_markup=keyboard)
        messages = await bot.send_media_group(
            chat_id=int(target_chat_id),
            media=[InputMediaPhoto(media=value, caption=body[:1024] if index == 0 else None) for index, value in enumerate(photo_values)],
        )
        if keyboard:
            await bot.send_message(chat_id=int(target_chat_id), text="Choose an option:", reply_markup=keyboard)
        return messages[0] if messages else SimpleNamespace(message_id=0)

    last_error: Exception | None = None
    for attempt in range(1, PUBLISH_MAX_RETRIES + 2):
        try:
            store.record_audit(owner_user_id, action, "attempting", int(draft.get("id")) if draft.get("id") else None, int(target_chat_id), batch_id, attempt)
            message = await send_once()
            message_id = getattr(message, "message_id", None)
            store.record_audit(owner_user_id, action, "success", int(draft.get("id")) if draft.get("id") else None, int(target_chat_id), batch_id, attempt, f"message_id={message_id}")
            return message
        except TelegramError as exc:
            last_error = exc
            store.record_audit(owner_user_id, action, "failed", int(draft.get("id")) if draft.get("id") else None, int(target_chat_id), batch_id, attempt, str(exc))
            if attempt <= PUBLISH_MAX_RETRIES:
                await asyncio.sleep(PUBLISH_RETRY_DELAY_SECONDS * attempt)
    raise TelegramError(str(last_error or "Telegram delivery failed"))


async def _publish_draft(bot: Any, store: ProviderPoolStore, draft: dict[str, Any], batch_id: str = "") -> dict[str, Any]:
    target = target_for(int(draft["channel_chat_id"])) if draft.get("channel_chat_id") is not None else None
    if not target:
        raise ProviderPoolError("The draft target is not in TARGETS_JSON")
    message = await _send_draft_with_retries(bot, store, draft, int(draft["channel_chat_id"]), "publish", batch_id)
    return store.update_draft(
        int(draft["owner_user_id"]),
        int(draft["id"]),
        status="published",
        published_at=datetime.now(timezone.utc),
        published_message_id=int(message.message_id),
        last_error="",
    )


async def draft_publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can publish drafts.")
        return
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 2:
        await update.message.reply_text("Usage: /publish <draft_id> <target_chat_id>")
        return
    store, owner_id = store_info
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        target_id = int(context.args[1])
        if not draft:
            raise ProviderPoolError("Draft not found")
        if draft["status"] != "approved":
            raise ProviderPoolError("Only APPROVED drafts can be published")
        if not target_for(target_id):
            raise ProviderPoolError("That target is not in TARGETS_JSON")
        draft = store.update_draft(owner_id, draft["id"], channel_chat_id=target_id)
        draft = await _publish_draft(context.bot, store, draft)
    except (ValueError, ProviderPoolError, TelegramError) as exc:
        await update.message.reply_text(f"The draft could not be published: {exc}")
        return
    await update.message.reply_text(f"✅ Draft #{draft['id']} published to `{draft['channel_chat_id']}`.")


async def publish_multi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can publish to multiple channels.")
        return
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 2:
        await update.message.reply_text("Usage: /publish_multi <draft_id> <target_chat_id_1,target_chat_id_2>")
        return
    store, owner_id = store_info
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        target_ids = [int(value.strip()) for value in context.args[1].split(",") if value.strip()]
        if not draft:
            raise ProviderPoolError("Draft not found")
        if draft["status"] != "approved":
            raise ProviderPoolError("Only APPROVED drafts can be published")
        if not target_ids or any(target_for(target_id) is None for target_id in target_ids):
            raise ProviderPoolError("Every target must exist in TARGETS_JSON")
        batch_id = uuid4().hex[:16]
        successes = 0
        failures: list[str] = []
        for target_id in target_ids:
            body = draft["post_text"]
            channel_profile = store.get_channel_profile(owner_id, target_id)
            if channel_profile and channel_profile.get("signature"):
                body = body.rstrip() + "\n\n" + channel_profile["signature"]
            try:
                await _send_text_with_retries(bot=context.bot, store=store, owner_user_id=owner_id, text=body, target_chat_id=target_id, action="publish_multi", draft_id=draft["id"], batch_id=batch_id)
                successes += 1
            except (TelegramError, ProviderPoolError) as exc:
                failures.append(f"{target_id}: {exc}")
        store.update_draft(owner_id, draft["id"], status="published" if successes and not failures else "failed", last_error="; ".join(failures))
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Multi-channel publishing could not start: {exc}")
        return
    status = f"✅ Batch `{batch_id}` completed: {successes}/{len(target_ids)} target(s) succeeded."
    if failures:
        status += "\nFailures:\n" + "\n".join(failures)[:1800]
    await update.message.reply_text(status)


async def batch_publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can run batch publishing.")
        return
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 2:
        await update.message.reply_text("Usage: /batch_publish <draft_id_1,draft_id_2> <target_chat_id>")
        return
    store, owner_id = store_info
    try:
        draft_ids = [int(value.strip()) for value in context.args[0].split(",") if value.strip()]
        target_id = int(context.args[1])
        if not target_for(target_id):
            raise ProviderPoolError("The target must exist in TARGETS_JSON")
        batch_id = uuid4().hex[:16]
        successes = 0
        failures: list[str] = []
        for draft_id in draft_ids:
            draft = store.get_draft(owner_id, draft_id)
            if not draft or draft["status"] != "approved":
                failures.append(f"draft {draft_id}: missing or not APPROVED")
                continue
            body = draft["post_text"]
            channel_profile = store.get_channel_profile(owner_id, target_id)
            if channel_profile and channel_profile.get("signature"):
                body = body.rstrip() + "\n\n" + channel_profile["signature"]
            try:
                await _send_text_with_retries(bot=context.bot, store=store, owner_user_id=owner_id, text=body, target_chat_id=target_id, action="batch_publish", draft_id=draft_id, batch_id=batch_id)
                store.update_draft(owner_id, draft_id, status="published", channel_chat_id=target_id, last_error="")
                successes += 1
            except (TelegramError, ProviderPoolError) as exc:
                failures.append(f"draft {draft_id}: {exc}")
                store.update_draft(owner_id, draft_id, status="failed", channel_chat_id=target_id, last_error=str(exc))
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"Batch publishing could not start: {exc}")
        return
    status = f"✅ Batch `{batch_id}` completed: {successes}/{len(draft_ids)} draft(s) published."
    if failures:
        status += "\nFailures:\n" + "\n".join(failures)[:1800]
    await update.message.reply_text(status)


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info:
        await update.message.reply_text("Audit logs are unavailable.")
        return
    store, owner_id = store_info
    rows = store.list_audit_logs(owner_id, limit=30)
    if not rows:
        await update.message.reply_text("No publishing audit records were found.")
        return
    lines = ["Recent publishing audit logs:"]
    for row in rows:
        created = row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"])
        lines.append(f"• {created} | {row['action']} | {row['status']} | draft={row['draft_id']} | target={row['target_chat_id']} | attempt={row['attempt']} | {row['detail'][:100]}")
    await update.message.reply_text("\n".join(lines)[:4000])


async def draft_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can schedule drafts.")
        return
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 3:
        await update.message.reply_text("Usage: /schedule <draft_id> <target_chat_id> <UTC ISO-8601 time>")
        return
    store, owner_id = store_info
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        target_id = int(context.args[1])
        scheduled_at = parse_utc_datetime(context.args[2])
        if not draft:
            raise ProviderPoolError("Draft not found")
        if draft["status"] != "approved":
            raise ProviderPoolError("Only APPROVED drafts can be scheduled")
        if not target_for(target_id):
            raise ProviderPoolError("That target is not in TARGETS_JSON")
        if scheduled_at <= datetime.now(timezone.utc):
            raise ProviderPoolError("The schedule time must be in the future")
        draft = store.update_draft(owner_id, draft["id"], status="scheduled", channel_chat_id=target_id, scheduled_at=scheduled_at, last_error="")
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The draft could not be scheduled: {exc}")
        return
    await update.message.reply_text(f"✅ Draft #{draft['id']} scheduled for {scheduled_at.isoformat()} UTC.")


async def channel_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can manage channel profiles.")
        return
    store_info = draft_store_required(update)
    payload = full_command_payload(update)
    left, separator, signature = payload.partition("||")
    parts = left.strip().split(" ", 2)
    if not store_info or len(parts) < 3:
        await update.message.reply_text("Usage: /channel_add <chat_id> <IANA_timezone> <label> || <signature>")
        return
    store, owner_id = store_info
    try:
        chat_id = int(parts[0])
        timezone_name = validate_timezone(parts[1])
        profile = store.upsert_channel_profile(owner_id, chat_id, parts[2], timezone_name, signature if separator else "")
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The channel profile could not be saved: {exc}")
        return
    await update.message.reply_text(f"✅ Channel profile `{profile['label']}` saved for `{profile['chat_id']}` in `{profile['timezone']}`.")


async def channel_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info:
        await update.message.reply_text("Channel profiles are unavailable.")
        return
    store, owner_id = store_info
    profiles = store.list_channel_profiles(owner_id)
    if not profiles:
        await update.message.reply_text("No channel profiles have been configured.")
        return
    lines = ["Channel profiles:"]
    for profile in profiles:
        signature = profile["signature"] or "(none)"
        lines.append(f"• {profile['label']} — {profile['chat_id']} — {profile['timezone']} — signature: {signature[:80]}")
    await update.message.reply_text("\n".join(lines))


async def channel_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can manage channel profiles.")
        return
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /channel_remove <chat_id>")
        return
    store, owner_id = store_info
    try:
        store.remove_channel_profile(owner_id, int(context.args[0]))
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The channel profile could not be removed: {exc}")
        return
    await update.message.reply_text("✅ Channel profile removed.")


async def template_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    parts = [part.strip() for part in full_command_payload(update).split("||")]
    if not store_info or len(parts) < 2:
        await update.message.reply_text("Usage: /template_add <name> || <template body> || <category> || <CTA>")
        return
    store, owner_id = store_info
    parts += [""] * (4 - len(parts))
    try:
        template = store.upsert_template(owner_id, parts[0], parts[1], parts[2], parts[3])
    except ProviderPoolError as exc:
        await update.message.reply_text(f"The template could not be saved: {exc}")
        return
    await update.message.reply_text(f"✅ Template `{template['name']}` saved.")


async def template_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info:
        await update.message.reply_text("Template storage is unavailable.")
        return
    store, owner_id = store_info
    templates = store.list_templates(owner_id)
    if not templates:
        await update.message.reply_text("No templates have been configured.")
        return
    lines = ["Templates:"]
    for template in templates:
        lines.append(f"• {template['name']} — {template['category'] or 'uncategorized'} — {template['body'][:100].replace(chr(10), ' ')}")
    await update.message.reply_text("\n".join(lines))


async def template_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /template_remove <name>")
        return
    store, owner_id = store_info
    try:
        store.remove_template(owner_id, context.args[0])
    except ProviderPoolError as exc:
        await update.message.reply_text(f"The template could not be removed: {exc}")
        return
    await update.message.reply_text("✅ Template removed.")


async def template_post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    payload = full_command_payload(update)
    parts = payload.split(" ", 1)
    if not store_info or len(parts) < 2:
        await update.message.reply_text("Usage: /template_post <template_name> <source text>")
        return
    store, owner_id = store_info
    template = store.get_template(owner_id, parts[0])
    if not template:
        await update.message.reply_text("Template not found.")
        return
    source = f"Use this template structure when creating the post:\n{template['body']}\n\nSource material:\n{parts[1]}"
    await update.message.reply_text("Preparing a template-based draft…")
    try:
        result = await ContentAgent(user_id=telegram_user_id(update)).create_post(source)
        draft = store.create_draft(owner_id, parts[1], result)
    except (AgentError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The template draft could not be created: {exc}")
        return
    await update.message.reply_text(f"✅ Template draft #{draft['id']} created. Use /preview {draft['id']} to review it.")


async def recurring_create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can create recurring posts.")
        return
    store_info = draft_store_required(update)
    if not store_info or len(context.args) < 4:
        await update.message.reply_text("Usage: /repeat <draft_id> <target_chat_id> <interval_minutes> <start_time> [until_time]")
        return
    store, owner_id = store_info
    try:
        draft = store.get_draft(owner_id, int(context.args[0]))
        target_id = int(context.args[1])
        interval = int(context.args[2])
        channel = store.get_channel_profile(owner_id, target_id)
        timezone_name = channel["timezone"] if channel else "UTC"
        start_at = parse_local_datetime(context.args[3], timezone_name)
        until_at = parse_local_datetime(context.args[4], timezone_name) if len(context.args) >= 5 else None
        if not draft:
            raise ProviderPoolError("Draft not found")
        if draft["status"] not in {"approved", "published"}:
            raise ProviderPoolError("Only APPROVED or PUBLISHED drafts can repeat")
        if not target_for(target_id):
            raise ProviderPoolError("That target is not in TARGETS_JSON")
        if start_at <= datetime.now(timezone.utc):
            raise ProviderPoolError("The start time must be in the future")
        recurring = store.create_recurring(owner_id, draft["id"], target_id, interval, start_at, until_at)
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The recurring post could not be created: {exc}")
        return
    await update.message.reply_text(f"✅ Recurring post #{recurring['id']} created. Next run: {start_at.isoformat()} UTC.")


async def recurring_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store_info = draft_store_required(update)
    if not store_info:
        await update.message.reply_text("Recurring-post storage is unavailable.")
        return
    store, owner_id = store_info
    rows = store.list_recurring(owner_id)
    if not rows:
        await update.message.reply_text("No recurring posts have been configured.")
        return
    lines = ["Recurring posts:"]
    for row in rows:
        state = "ACTIVE" if row["active"] else "PAUSED/COMPLETED"
        lines.append(f"• #{row['id']} — {state} — draft #{row['draft_id']} — every {row['interval_minutes']} minutes — next {row['next_run_at']}")
    await update.message.reply_text("\n".join(lines))


async def recurring_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can remove recurring posts.")
        return
    store_info = draft_store_required(update)
    if not store_info or not context.args:
        await update.message.reply_text("Usage: /repeat_remove <recurring_id>")
        return
    store, owner_id = store_info
    try:
        store.remove_recurring(owner_id, int(context.args[0]))
    except (ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The recurring post could not be removed: {exc}")
        return
    await update.message.reply_text("✅ Recurring post removed.")


async def curate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source = command_payload(update, context)
    if not source:
        await update.message.reply_text("Enter content to curate, or reply to a message and use /curate.")
        return
    target = {
        "label": "Unspecified target",
        "description": "No target has been specified; suggest only a category and context.",
        "allowed_categories": [],
    }
    try:
        result = await ContentAgent(user_id=telegram_user_id(update)).curate(source, target)
        status = "Potentially relevant" if result.get("should_forward") else "Uncertain / do not forward"
        await update.message.reply_text(
            f"📌 Category: {result.get('category', 'Unknown')}\n"
            f"📊 Decision: {status}\n\n"
            f"{result.get('intro', '')}\n\n"
            f"Reason: {result.get('reason', '')}"
        )
    except AgentError as exc:
        await update.message.reply_text(f"The request could not be completed: {exc}")


async def groupscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_scan_allowed(update):
        await update.message.reply_text("This chat is not included in the GroupScan allowlist.")
        return
    try:
        raw = await group_scan_payload(update, context)
    except GroupScanInputError as exc:
        await update.message.reply_text(f"Invalid GroupScan input: {exc}")
        return
    if not raw:
        await update.message.reply_text(
            "Usage:\n"
            "/groupscan AI tools\n"
            "AI Myanmar | AI tools discussion | 12K\n"
            "Marketing MM | Digital marketing | 850\n\n"
            "Alternatively, upload a `.txt`, `.csv`, or `.json` group list and reply to it with /groupscan <niche>."
        )
        return

    niche, group_text = split_niche_and_groups(raw)
    if not niche and PROVIDER_POOL is not None and telegram_user_id(update) is not None:
        niche = PROVIDER_POOL.get_preference(telegram_user_id(update), "default_niche", "")
    try:
        groups = parse_group_records(group_text)
    except GroupScanInputError as exc:
        await update.message.reply_text(f"Invalid GroupScan input: {exc}")
        return
    if not groups:
        await update.message.reply_text(
            "No group records were found. Use `Group Name | description | member count`, a CSV header, or a JSON groups format."
        )
        return

    status_msg = await update.message.reply_text(
        f"Evaluating {len(groups)} group(s) for `{niche or 'unspecified niche'}`…"
    )
    try:
        result = await ContentAgent(user_id=telegram_user_id(update)).scout(groups, niche)
        report = render_report(result)
        await status_msg.delete()
        for part in chunk_text(report):
            await update.message.reply_text(part)
    except AgentError as exc:
        await status_msg.edit_text(f"The GroupScan request could not be completed: {exc}")
    except TelegramError:
        logger.exception("Could not update GroupScan status message")


# Backward-compatible command name.
scout_command = groupscan_command


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    await update.message.reply_text(
        f"📌 Chat ID: `{chat.id}`\n\n"
        "Add this ID to `GROUPSCAN_ALLOWED_CHAT_IDS` to restrict GroupScan to approved chats."
    )


async def targets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    if not TARGETS:
        await update.message.reply_text("TARGETS_JSON is not configured.")
        return
    lines = ["Allowed forwarding targets:"]
    for target in TARGETS:
        categories = ", ".join(target["allowed_categories"]) or "not specified"
        lines.append(f"• {target['label']} ({target['chat_id']}) — {categories}\n  {target['description']}")
    await update.message.reply_text("\n".join(lines))


async def forward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use smart forwarding.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the source message and use `/forward <target_chat_id>`.")
        return
    if not context.args:
        await update.message.reply_text("Enter a target chat ID. Use `/targets` to see allowed targets.")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The target chat ID must be a number.")
        return
    target = target_for(target_id)
    if not target:
        await update.message.reply_text("That target is not in the allowlist.")
        return
    source = source_from_message(update.message.reply_to_message)
    if not source:
        await update.message.reply_text("The source message has no text or caption, so safe curation cannot be performed.")
        return

    await update.message.reply_text("Checking target relevance before forwarding…")
    try:
        decision = await ContentAgent(user_id=telegram_user_id(update)).curate(source, target)
        if not decision.get("should_forward") or decision.get("needs_review"):
            await update.message.reply_text(
                "Not forwarded.\n"
                f"Reason: {decision.get('reason', 'Relevance is uncertain.')}"
            )
            return
        intro = str(decision.get("intro", "")).strip()
        if not intro:
            await update.message.reply_text("No context intro was generated, so the raw message was not forwarded.")
            return
        await context.bot.send_message(chat_id=target_id, text=intro)
        await context.bot.forward_message(
            chat_id=target_id,
            from_chat_id=update.message.reply_to_message.chat_id,
            message_id=update.message.reply_to_message.message_id,
        )
        await update.message.reply_text(f"✅ Forwarded to {target['label']} with a context intro.")
    except (AgentError, TelegramError) as exc:
        logger.exception("Forwarding failed")
        await update.message.reply_text(f"Forwarding failed: {exc}")


async def _publish_recurring(bot: Any, store: ProviderPoolStore, recurring: dict[str, Any]) -> dict[str, Any]:
    draft = store.get_draft_by_id(int(recurring["draft_id"]))
    if not draft:
        raise ProviderPoolError("The recurring post draft was not found")
    if not target_for(int(recurring["channel_chat_id"])):
        raise ProviderPoolError("The recurring target is not in TARGETS_JSON")
    send_draft = {**draft, "owner_user_id": recurring["owner_user_id"], "channel_chat_id": recurring["channel_chat_id"]}
    return await _send_draft_with_retries(bot, store, send_draft, int(recurring["channel_chat_id"]), "recurring")


async def scheduled_publish_loop(application: Application) -> None:
    while True:
        try:
            if PROVIDER_POOL is not None:
                due_drafts = PROVIDER_POOL.claim_due_drafts()
                for draft in due_drafts:
                    try:
                        await _publish_draft(application.bot, PROVIDER_POOL, draft)
                        logger.info("Published scheduled draft %s", draft["id"])
                    except (ProviderPoolError, TelegramError) as exc:
                        logger.exception("Scheduled draft %s failed", draft["id"])
                        PROVIDER_POOL.update_draft(
                            int(draft["owner_user_id"]),
                            int(draft["id"]),
                            status="failed",
                            last_error=str(exc)[:1000],
                        )
                due_recurring = PROVIDER_POOL.claim_due_recurring()
                for recurring in due_recurring:
                    run_at = datetime.now(timezone.utc)
                    try:
                        message = await _publish_recurring(application.bot, PROVIDER_POOL, recurring)
                        next_run = run_at + timedelta(minutes=int(recurring["interval_minutes"]))
                        until_at = recurring.get("until_at")
                        if until_at and next_run > until_at:
                            next_run = None
                        PROVIDER_POOL.complete_recurring(int(recurring["id"]), next_run, run_at, "")
                        logger.info("Published recurring post %s as Telegram message %s", recurring["id"], message.message_id)
                    except (ProviderPoolError, TelegramError) as exc:
                        logger.exception("Recurring post %s failed", recurring["id"])
                        retry_at = run_at + timedelta(minutes=5)
                        until_at = recurring.get("until_at")
                        if until_at and retry_at > until_at:
                            retry_at = None
                        PROVIDER_POOL.complete_recurring(int(recurring["id"]), retry_at, run_at, str(exc)[:1000])
        except Exception:
            logger.exception("Scheduled publishing worker iteration failed")
        await asyncio.sleep(30)


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.pop("scheduled_publish_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)


async def post_init(application: Application) -> None:
    application.bot_data["scheduled_publish_task"] = asyncio.create_task(scheduled_publish_loop(application))
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("agent", "Ask the AI agent"),
            BotCommand("post", "Create a draft from source material"),
            BotCommand("draft_list", "List your drafts"),
            BotCommand("preview", "Preview a draft"),
            BotCommand("draft_edit", "Edit a draft"),
            BotCommand("approve", "Approve a draft"),
            BotCommand("publish", "Publish an approved draft"),
            BotCommand("schedule", "Schedule an approved draft"),
            BotCommand("channel_add", "Add a channel profile"),
            BotCommand("channel_list", "List channel profiles"),
            BotCommand("channel_remove", "Remove a channel profile"),
            BotCommand("template_add", "Add a content template"),
            BotCommand("template_list", "List content templates"),
            BotCommand("template_post", "Create a template draft"),
            BotCommand("template_remove", "Remove a content template"),
            BotCommand("repeat", "Create a recurring post"),
            BotCommand("repeat_list", "List recurring posts"),
            BotCommand("repeat_remove", "Remove a recurring post"),
            BotCommand("publish_multi", "Publish to multiple channels"),
            BotCommand("batch_publish", "Run batch publishing"),
            BotCommand("audit", "Show publishing audit logs"),
            BotCommand("slideshow_add", "Add a slideshow photo"),
            BotCommand("curate", "Content curation"),
            BotCommand("groupscan", "GroupScan scouting"),
            BotCommand("scout", "GroupScan alias"),
            BotCommand("id", "Show the current chat ID"),
            BotCommand("forward", "Smart forwarding"),
            BotCommand("targets", "Target list"),
            BotCommand("provider_list", "API provider list"),
            BotCommand("provider_add", "Add API provider"),
            BotCommand("provider_use", "Select API provider"),
            BotCommand("provider_test", "Test API provider"),
            BotCommand("provider_remove", "Remove API provider"),
            BotCommand("preferences", "Show saved preferences"),
            BotCommand("prefs_set", "Save a preference"),
            BotCommand("help", "Show help"),
        ]
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required")
    if not LLM_API_KEY and PROVIDER_POOL is None and PROVIDER_POOL_ERROR:
        raise SystemExit(PROVIDER_POOL_ERROR)
    if not LLM_API_KEY and PROVIDER_POOL is None and not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required")

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("agent", agent_command))
    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("draft_list", draft_list_command))
    application.add_handler(CommandHandler("preview", draft_preview_command))
    application.add_handler(CommandHandler("draft_edit", draft_edit_command))
    application.add_handler(CommandHandler("approve", draft_approve_command))
    application.add_handler(CommandHandler("publish", draft_publish_command))
    application.add_handler(CommandHandler("schedule", draft_schedule_command))
    application.add_handler(CommandHandler("channel_add", channel_add_command))
    application.add_handler(CommandHandler("channel_list", channel_list_command))
    application.add_handler(CommandHandler("channel_remove", channel_remove_command))
    application.add_handler(CommandHandler("template_add", template_add_command))
    application.add_handler(CommandHandler("template_list", template_list_command))
    application.add_handler(CommandHandler("template_post", template_post_command))
    application.add_handler(CommandHandler("template_remove", template_remove_command))
    application.add_handler(CommandHandler("repeat", recurring_create_command))
    application.add_handler(CommandHandler("repeat_list", recurring_list_command))
    application.add_handler(CommandHandler("repeat_remove", recurring_remove_command))
    application.add_handler(CommandHandler("publish_multi", publish_multi_command))
    application.add_handler(CommandHandler("batch_publish", batch_publish_command))
    application.add_handler(CommandHandler("audit", audit_command))
    application.add_handler(CommandHandler("slideshow_add", slideshow_add_command))
    application.add_handler(CommandHandler("curate", curate_command))
    application.add_handler(CommandHandler("groupscan", groupscan_command))
    application.add_handler(CommandHandler("scout", scout_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("targets", targets_command))
    application.add_handler(CommandHandler("provider_list", provider_list_command))
    application.add_handler(CommandHandler("provider_use", provider_use_command))
    application.add_handler(CommandHandler("provider_test", provider_test_command))
    application.add_handler(CommandHandler("provider_remove", provider_remove_command))
    application.add_handler(CommandHandler("preferences", preferences_command))
    application.add_handler(CommandHandler("prefs_set", preferences_set_command))
    provider_add_handler = ConversationHandler(
        entry_points=[CommandHandler("provider_add", provider_add_start)],
        states={
            PROVIDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, provider_add_name)],
            PROVIDER_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, provider_add_key)],
            PROVIDER_ENDPOINT: [MessageHandler(filters.TEXT & ~filters.COMMAND, provider_add_endpoint)],
            PROVIDER_MODEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, provider_add_model)],
            PROVIDER_OPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, provider_add_options)],
        },
        fallbacks=[CommandHandler("cancel", provider_add_cancel)],
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )
    application.add_handler(provider_add_handler)
    application.add_handler(CommandHandler("forward", forward_command))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, freeform_agent_message))
    application.add_error_handler(error_handler)
    active_name = PROVIDER_POOL.active_name if PROVIDER_POOL is not None else "environment fallback"
    logger.info("Starting Telegram Content Strategist bot with provider %s", active_name or "environment fallback")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
