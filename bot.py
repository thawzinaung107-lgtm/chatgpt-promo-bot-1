"""Telegram AI Content Strategist and Growth Manager.

The bot intentionally contains only content strategy workflows. It does not scan
credentials, access tokens, promo codes, or private account data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from telegram import BotCommand, Update
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
PROVIDER_POOL: ProviderPoolStore | None = None
PROVIDER_POOL_ERROR = ""
try:
    PROVIDER_POOL = ProviderPoolStore.from_environment()
except ProviderPoolError as exc:
    PROVIDER_POOL_ERROR = str(exc)
    logger.error("Provider pool disabled: %s", exc)


class AgentError(RuntimeError):
    """Raised when the model cannot provide a safe structured answer."""


class ContentAgent:
    def __init__(self, provider: ProviderProfile | None = None) -> None:
        selected = provider
        if selected is None and PROVIDER_POOL is not None:
            try:
                selected = PROVIDER_POOL.get()
            except ProviderPoolError as exc:
                logger.warning("Active provider profile is invalid; using environment fallback: %s", exc)
        self.provider_name = selected.name if selected else "environment"
        self.api_key = selected.api_key if selected else LLM_API_KEY
        self.base_url = normalize_base_url(selected.base_url) if selected else LLM_BASE_URL
        self.model = selected.model if selected else LLM_MODEL
        self.response_format = selected.response_format if selected else (os.getenv("LLM_RESPONSE_FORMAT", "auto") or "auto").strip().lower()
        self.max_tokens_param = selected.max_tokens_param if selected else (os.getenv("LLM_MAX_TOKENS_PARAM", "auto") or "auto").strip().lower()
        self.timeout_seconds = selected.timeout_seconds if selected else LLM_TIMEOUT_SECONDS
        self.max_retries = selected.max_retries if selected else LLM_MAX_RETRIES
        self.reasoning_effort = selected.reasoning_effort if selected else LLM_REASONING_EFFORT
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
                    {"role": "system", "content": SYSTEM_IDENTITY},
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


def provider_list_text() -> str:
    if PROVIDER_POOL is None:
        detail = PROVIDER_POOL_ERROR or "BOT_TOKEN is not configured."
        return f"The provider pool is unavailable. {detail}"
    profiles = PROVIDER_POOL.list_profiles()
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
        if store is None:
            raise ProviderPoolError("Provider pool is not available")
        store.upsert(profile)
    except (KeyError, ValueError, ProviderPoolError) as exc:
        await update.message.reply_text(f"The provider profile could not be saved: {exc}\nUse /cancel and restart with /provider_add.")
        context.user_data.pop("provider_draft", None)
        return ConversationHandler.END
    context.user_data.pop("provider_draft", None)
    await update.message.reply_text(
        f"✅ Provider `{profile.name}` has been saved.\n"
        "The API key will be masked in listings. Use /provider_use "
        f"{profile.name} to activate it."
    )
    return ConversationHandler.END


async def provider_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("provider_draft", None)
    await update.message.reply_text("Provider setup has been cancelled.")
    return ConversationHandler.END


async def provider_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    await update.message.reply_text(provider_list_text())


async def provider_use_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /provider_use <profile_name>")
        return
    store = provider_store_or_error()
    if store is None:
        await update.message.reply_text(provider_list_text())
        return
    try:
        profile = store.activate(context.args[0].strip())
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
        await update.message.reply_text(provider_list_text())
        return
    try:
        store.remove(context.args[0].strip())
    except ProviderPoolError as exc:
        await update.message.reply_text(f"The provider could not be removed: {exc}")
        return
    await update.message.reply_text(f"✅ Provider `{context.args[0].strip()}` has been removed.\n\n{provider_list_text()}")


async def provider_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Only bot administrators can use this command.")
        return
    store = provider_store_or_error()
    try:
        profile = store.get(context.args[0].strip()) if context.args and store else None
        if context.args and profile is None:
            raise ProviderPoolError(f"Provider '{context.args[0].strip()}' was not found")
        selected_name = profile.name if profile else "environment fallback"
        await update.message.reply_text(f"Testing `{selected_name}`…")
        result = await ContentAgent(provider=profile).agent(
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
        result = await ContentAgent().agent(prompt)
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
        result = await ContentAgent().create_post(source)
        review = "\n\n⚠️ Note: The source information is incomplete; review it before publishing." if result.get("needs_review") else ""
        await update.message.reply_text(result["post"][:4000] + review)
    except AgentError as exc:
        await update.message.reply_text(f"The request could not be completed: {exc}")


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
        result = await ContentAgent().curate(source, target)
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
        result = await ContentAgent().scout(groups, niche)
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
        decision = await ContentAgent().curate(source, target)
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("agent", "Ask the AI agent"),
            BotCommand("post", "Create a Telegram post"),
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

    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("agent", agent_command))
    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("curate", curate_command))
    application.add_handler(CommandHandler("groupscan", groupscan_command))
    application.add_handler(CommandHandler("scout", scout_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("targets", targets_command))
    application.add_handler(CommandHandler("provider_list", provider_list_command))
    application.add_handler(CommandHandler("provider_use", provider_use_command))
    application.add_handler(CommandHandler("provider_test", provider_test_command))
    application.add_handler(CommandHandler("provider_remove", provider_remove_command))
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
