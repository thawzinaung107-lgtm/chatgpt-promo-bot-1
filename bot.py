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
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from groupscan import (
    GroupScanInputError,
    MAX_FILE_BYTES as GROUPSCAN_MAX_FILE_BYTES,
    parse_groups as parse_group_records,
    parse_member_count,
    render_report,
    split_niche_and_groups,
)


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
3. For curation or forwarding, write a compelling 1-2 sentence Burmese intro that
   adds context. Never forward content blindly.
4. Evaluate Telegram groups only from their supplied name, description, and member
   count. Strictly flag spam, irrelevant niches, and groups whose supplied evidence
   indicates low quality.

Non-negotiable rules:
- Output Burmese by default unless the user explicitly requests English.
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


class AgentError(RuntimeError):
    """Raised when the model cannot provide a safe structured answer."""


class ContentAgent:
    def __init__(self) -> None:
        if not LLM_API_KEY:
            raise AgentError("LLM_API_KEY or OPENAI_API_KEY is not configured")
        client_kwargs: dict[str, Any] = {
            "api_key": LLM_API_KEY,
            "timeout": LLM_TIMEOUT_SECONDS,
            "max_retries": LLM_MAX_RETRIES,
        }
        if LLM_BASE_URL:
            client_kwargs["base_url"] = LLM_BASE_URL
        self.client = OpenAI(**client_kwargs)

    def _response_modes(self) -> list[str]:
        configured = (os.getenv("LLM_RESPONSE_FORMAT", "auto") or "auto").strip().lower()
        if configured in {"json_schema", "json_object", "none"}:
            return [configured]
        # Many OpenAI-compatible providers implement JSON mode but not strict JSON
        # schema. Try the strongest contract first, then degrade safely.
        return ["json_schema", "json_object", "none"]

    def _token_parameter(self, max_tokens: int) -> dict[str, int]:
        configured = (os.getenv("LLM_MAX_TOKENS_PARAM", "auto") or "auto").strip().lower()
        if configured == "max_tokens":
            return {"max_tokens": max_tokens}
        if configured == "max_completion_tokens":
            return {"max_completion_tokens": max_tokens}
        model = LLM_MODEL.lower()
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
                "model": LLM_MODEL,
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
            if LLM_REASONING_EFFORT and LLM_MODEL.lower().rsplit("/", 1)[-1].startswith("gpt-5"):
                request["extra_body"] = {"reasoning": {"effort": LLM_REASONING_EFFORT}}

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
and Growth Manager. Default to Burmese. Choose the most useful mode, such as
content_creation, curation, campaign_planning, audience_growth, or group_scouting.
Use only facts supplied in the request. If the user asks for unsupported news,
statistics, engagement, or audience claims, explain the limitation and set
needs_review to true. Keep the answer practical, mobile-friendly, and professional.

USER REQUEST:
{prompt}
""",
        )

    async def create_post(self, source: str, language: str = "Burmese") -> dict[str, Any]:
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

If relevant, write a compelling 1-2 sentence Burmese intro that adds context before
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


def usage_text() -> str:
    return (
        "အသုံးပြုနိုင်သော command များ\n\n"
        "/agent <request> — AI Content Strategist အဖြစ် general request ဖြေရှင်းရန်\n"
        "/post <အချက်အလက်> — source ကို Burmese Telegram post အဖြစ်ရေးရန်\n"
        "/curate <content> — target မသတ်မှတ်ဘဲ context intro နှင့် category အကြံပြုရန်\n"
        "/groupscan <niche>\nအုပ်စုအမည် | description | members — group များစစ်ရန်\n"
        "/scout — /groupscan ၏ backward-compatible alias\n"
        "/id — လက်ရှိ chat ID ကြည့်ရန်\n"
        "/forward <target_chat_id> — reply လုပ်ထားသော message ကို relevance စစ်ပြီး intro နှင့် forward လုပ်ရန်\n"
        "/targets — ခွင့်ပြုထားသော target channel/group များကြည့်ရန်\n"
        "/help — အကူအညီ\n\n"
        "Content ကို command နောက်တွင်ထည့်နိုင်သလို၊ message တစ်ခုကို reply လုပ်ပြီး command သုံးနိုင်ပါတယ်။"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "မင်္ဂလာပါ။ ကျွန်ုပ်သည် Telegram Content Strategist & Growth Manager ဖြစ်ပါတယ်။\n\n"
        "Raw information ကို mobile-friendly Burmese post အဖြစ်ပြောင်းပေးနိုင်ပြီး၊ content curation၊ smart forwarding နဲ့ target group evaluation ကိုလည်း လုပ်ပေးနိုင်ပါတယ်။\n\n"
        + usage_text()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(usage_text())


async def run_agent_reply(update: Update, prompt: str) -> None:
    if not prompt:
        await update.message.reply_text("AI agent ကို မေးလိုသော request ထည့်ပါ။")
        return
    await update.message.reply_text("AI agent က စီစဉ်ပြီး ဖြေဆိုနေပါတယ်…")
    try:
        result = await ContentAgent().agent(prompt)
        review = "\n\n⚠️ မှတ်ချက် — source အချက်အလက် မပြည့်စုံသဖြင့် ပြန်စစ်ရန်လိုပါသည်။" if result.get("needs_review") else ""
        answer = str(result.get("answer", "")).strip() or "ဖြေဆိုချက် မရရှိသေးပါ။"
        for part in chunk_text(answer + review):
            await update.message.reply_text(part)
    except AgentError as exc:
        await update.message.reply_text(f"AI agent မလုပ်ဆောင်နိုင်သေးပါ။ {exc}")


async def agent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    prompt = full_command_payload(update)
    reply = update.message.reply_to_message if update.message else None
    reply_text = source_from_message(reply)
    if reply_text:
        prompt = f"{prompt}\n\nREFERENCE CONTENT:\n{reply_text}".strip() if prompt else reply_text
    if not prompt:
        await update.message.reply_text("AI agent ကို မေးလိုသော request ထည့်ပါ သို့မဟုတ် message တစ်ခုကို reply လုပ်ပြီး /agent ကိုသုံးပါ။")
        return
    await run_agent_reply(update, prompt)


async def freeform_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type == "private":
        await run_agent_reply(update, source_from_message(update.message))


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source = command_payload(update, context)
    if not source:
        await update.message.reply_text("ရေးသားရန် source text ထည့်ပါ သို့မဟုတ် message တစ်ခုကို reply လုပ်ပြီး /post ကိုသုံးပါ။")
        return
    await update.message.reply_text("Content ကို စီစဉ်နေပါတယ်…")
    try:
        result = await ContentAgent().create_post(source)
        review = "\n\n⚠️ မှတ်ချက် — source အချက်အလက် မပြည့်စုံသဖြင့် publish မလုပ်မီ ပြန်စစ်ပါ။" if result.get("needs_review") else ""
        await update.message.reply_text(result["post"][:4000] + review)
    except AgentError as exc:
        await update.message.reply_text(f"မလုပ်ဆောင်နိုင်သေးပါ။ {exc}")


async def curate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    source = command_payload(update, context)
    if not source:
        await update.message.reply_text("စီစစ်ရန် content ထည့်ပါ သို့မဟုတ် message တစ်ခုကို reply လုပ်ပြီး /curate ကိုသုံးပါ။")
        return
    target = {
        "label": "သတ်မှတ်မထားသော target",
        "description": "Target မသတ်မှတ်ရသေးပါ။ Category နှင့် context ကိုသာ အကြံပြုပါ။",
        "allowed_categories": [],
    }
    try:
        result = await ContentAgent().curate(source, target)
        status = "သင့်လျော်နိုင်သည်" if result.get("should_forward") else "မသေချာသေးပါ / မပို့သင့်ပါ"
        await update.message.reply_text(
            f"📌 Category: {result.get('category', 'မသတ်မှတ်နိုင်')}\n"
            f"📊 ဆုံးဖြတ်ချက်: {status}\n\n"
            f"{result.get('intro', '')}\n\n"
            f"အကြောင်းပြချက်: {result.get('reason', '')}"
        )
    except AgentError as exc:
        await update.message.reply_text(f"မလုပ်ဆောင်နိုင်သေးပါ။ {exc}")


async def groupscan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not group_scan_allowed(update):
        await update.message.reply_text("ဒီ chat ကို GroupScan အသုံးပြုခွင့် allowlist ထဲတွင် မထည့်ရသေးပါ။")
        return
    try:
        raw = await group_scan_payload(update, context)
    except GroupScanInputError as exc:
        await update.message.reply_text(f"GroupScan input မမှန်ပါ။ {exc}")
        return
    if not raw:
        await update.message.reply_text(
            "အသုံးပြုပုံ:\n"
            "/groupscan AI tools\n"
            "AI Myanmar | AI tool များဆွေးနွေးခြင်း | 12K\n"
            "Marketing MM | Digital marketing | 850\n\n"
            "သို့မဟုတ် group list `.txt`, `.csv`, `.json` ကို upload လုပ်ပြီး reply ဖြင့် /groupscan <niche> ကိုသုံးပါ။"
        )
        return

    niche, group_text = split_niche_and_groups(raw)
    try:
        groups = parse_group_records(group_text)
    except GroupScanInputError as exc:
        await update.message.reply_text(f"GroupScan input မမှန်ပါ။ {exc}")
        return
    if not groups:
        await update.message.reply_text(
            "Group record မတွေ့ပါ။ `အမည် | description | member count`၊ CSV header သို့မဟုတ် JSON groups format ကိုသုံးပါ။"
        )
        return

    status_msg = await update.message.reply_text(
        f"{len(groups)} ခုကို `{niche or 'သတ်မှတ်မထားသော niche'}` အတွက် စစ်နေပါတယ်…"
    )
    try:
        result = await ContentAgent().scout(groups, niche)
        report = render_report(result)
        await status_msg.delete()
        for part in chunk_text(report):
            await update.message.reply_text(part)
    except AgentError as exc:
        await status_msg.edit_text(f"မလုပ်ဆောင်နိုင်သေးပါ။ {exc}")
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
        "ဒီ ID ကို `GROUPSCAN_ALLOWED_CHAT_IDS` ထဲထည့်ပြီး GroupScan ကို သတ်မှတ်ထားတဲ့ chat များတွင်သာ ခွင့်ပြုနိုင်ပါတယ်။"
    )


async def targets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("ဒီ command ကို bot admin များသာ အသုံးပြုနိုင်ပါတယ်။")
        return
    if not TARGETS:
        await update.message.reply_text("TARGETS_JSON မသတ်မှတ်ရသေးပါ။")
        return
    lines = ["ခွင့်ပြုထားသော target များ:"]
    for target in TARGETS:
        categories = ", ".join(target["allowed_categories"]) or "မသတ်မှတ်ထား"
        lines.append(f"• {target['label']} ({target['chat_id']}) — {categories}\n  {target['description']}")
    await update.message.reply_text("\n".join(lines))


async def forward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Smart forwarding ကို bot admin များသာ အသုံးပြုနိုင်ပါတယ်။")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Forward လုပ်မည့် source message ကို reply လုပ်ပြီး `/forward <target_chat_id>` ကိုသုံးပါ။")
        return
    if not context.args:
        await update.message.reply_text("Target chat ID ထည့်ပါ။ `/targets` ဖြင့် ခွင့်ပြုထားသော target များကြည့်နိုင်ပါတယ်။")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Target chat ID သည် number ဖြစ်ရပါမယ်။")
        return
    target = target_for(target_id)
    if not target:
        await update.message.reply_text("ဒီ target ကို allowlist ထဲတွင် မတွေ့ပါ။")
        return
    source = source_from_message(update.message.reply_to_message)
    if not source:
        await update.message.reply_text("Source message တွင် text သို့မဟုတ် caption မရှိသဖြင့် safe curation မလုပ်နိုင်ပါ။")
        return

    await update.message.reply_text("Target relevance ကို စစ်ပြီး forward လုပ်နေပါတယ်…")
    try:
        decision = await ContentAgent().curate(source, target)
        if not decision.get("should_forward") or decision.get("needs_review"):
            await update.message.reply_text(
                "မပို့သေးပါ။\n"
                f"အကြောင်းပြချက်: {decision.get('reason', 'မသေချာသော relevance')}"
            )
            return
        intro = str(decision.get("intro", "")).strip()
        if not intro:
            await update.message.reply_text("Context intro မရသဖြင့် raw forward မလုပ်ပါ။")
            return
        await context.bot.send_message(chat_id=target_id, text=intro)
        await context.bot.forward_message(
            chat_id=target_id,
            from_chat_id=update.message.reply_to_message.chat_id,
            message_id=update.message.reply_to_message.message_id,
        )
        await update.message.reply_text(f"✅ {target['label']} သို့ context intro ဖြင့် forward လုပ်ပြီးပါပြီ။")
    except (AgentError, TelegramError) as exc:
        logger.exception("Forwarding failed")
        await update.message.reply_text(f"Forward မအောင်မြင်ပါ။ {exc}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled update error", exc_info=context.error)


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "စတင်ရန်"),
            BotCommand("agent", "AI agent ကိုမေးရန်"),
            BotCommand("post", "Telegram post ရေးရန်"),
            BotCommand("curate", "Content curation"),
            BotCommand("groupscan", "GroupScan scouting"),
            BotCommand("scout", "GroupScan alias"),
            BotCommand("id", "လက်ရှိ chat ID"),
            BotCommand("forward", "Smart forwarding"),
            BotCommand("targets", "Target list"),
            BotCommand("help", "အကူအညီ"),
        ]
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required")
    if not LLM_API_KEY:
        raise SystemExit("LLM_API_KEY or OPENAI_API_KEY is required")

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
    application.add_handler(CommandHandler("forward", forward_command))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, freeform_agent_message))
    application.add_error_handler(error_handler)
    logger.info("Starting Telegram Content Strategist bot with model %s", LLM_MODEL)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
