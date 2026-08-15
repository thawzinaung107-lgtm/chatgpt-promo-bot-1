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

from openai import OpenAI
from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes


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
LLM_BASE_URL = (os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_API_BASE", "")).strip()
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5-mini").strip()
ADMIN_IDS = _csv_ints(os.getenv("ADMIN_IDS"))
TARGETS = _load_targets(os.getenv("TARGETS_JSON"))
MAX_INPUT_CHARS = max(1000, int(os.getenv("MAX_INPUT_CHARS", "8000")))


class AgentError(RuntimeError):
    """Raised when the model cannot provide a safe structured answer."""


class ContentAgent:
    def __init__(self) -> None:
        if not LLM_API_KEY:
            raise AgentError("LLM_API_KEY or OPENAI_API_KEY is not configured")
        client_kwargs: dict[str, Any] = {"api_key": LLM_API_KEY}
        if LLM_BASE_URL:
            client_kwargs["base_url"] = LLM_BASE_URL
        self.client = OpenAI(**client_kwargs)

    async def _structured(
        self, *, task: str, schema_name: str, schema: dict[str, Any], max_tokens: int = 2200
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_IDENTITY},
                {"role": "user", "content": task[:MAX_INPUT_CHARS]},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "max_completion_tokens": max_tokens,
        }
        try:
            response = await asyncio.to_thread(
                self.client.chat.completions.create, **request
            )
        except Exception as exc:
            logger.exception("LLM request failed")
            raise AgentError("The language model request failed") from exc

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise AgentError("The language model returned an empty response")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error("Model returned invalid JSON: %r", content[:300])
            raise AgentError("The language model returned invalid structured data") from exc
        if not isinstance(data, dict):
            raise AgentError("The language model returned an unexpected shape")
        return data

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
        return await self._structured(
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


def parse_member_count(raw: Any) -> int | None:
    if isinstance(raw, (int, float)):
        return int(raw)
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def parse_groups(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = parsed.get("groups", [])
        if isinstance(parsed, list):
            groups = []
            for item in parsed:
                if isinstance(item, dict) and item.get("name"):
                    groups.append(
                        {
                            "name": str(item["name"]).strip(),
                            "description": str(item.get("description", "")).strip(),
                            "member_count": parse_member_count(item.get("member_count")),
                        }
                    )
            if groups:
                return groups[:30]
    except json.JSONDecodeError:
        pass

    groups = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in re.split(r"\s*\|\s*|\t+", line)]
        if len(parts) < 2:
            continue
        name = parts[0]
        member_count = parse_member_count(parts[-1]) if len(parts) >= 3 else None
        description = " | ".join(parts[1:-1]) if len(parts) >= 3 else parts[1]
        groups.append(
            {"name": name, "description": description, "member_count": member_count}
        )
    return groups[:30]


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
        "/post <အချက်အလက်> — source ကို Burmese Telegram post အဖြစ်ရေးရန်\n"
        "/curate <content> — target မသတ်မှတ်ဘဲ context intro နှင့် category အကြံပြုရန်\n"
        "/scout <niche>\nအုပ်စုအမည် | description | members — group များစစ်ရန်\n"
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


async def scout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args).strip()
    if update.message.reply_to_message:
        raw = source_from_message(update.message.reply_to_message)
    if not raw:
        await update.message.reply_text(
            "ဥပမာ:\n/scout AI tools\nAI Myanmar | AI tool များဆွေးနွေးခြင်း | 12K\nMarketing MM | Digital marketing | 850"
        )
        return

    niche = ""
    lines = raw.splitlines()
    if lines and "|" not in lines[0] and "\t" not in lines[0] and not lines[0].lstrip().startswith("{"):
        niche, raw = lines[0], "\n".join(lines[1:])
    groups = parse_groups(raw)
    if not groups:
        await update.message.reply_text("Group format မမှန်ပါ။ `အမည် | description | member count` တစ်ကြောင်းစီသုံးပါ။")
        return

    await update.message.reply_text(f"{len(groups)} ခုကို target niche နဲ့ တိုက်စစ်နေပါတယ်…")
    try:
        result = await ContentAgent().scout(groups, niche)
        rows = ["🔎 Group Scouting Report", ""]
        for item in result.get("groups", []):
            score = max(0, min(100, float(item.get("fit_score", 0))))
            flags = []
            if item.get("spam_flag"):
                flags.append("SPAM FLAG")
            if not item.get("match"):
                flags.append("IRRELEVANT")
            label = item.get("quality_label", "unknown").upper()
            action = item.get("action", "review").upper()
            rows.append(f"• {item.get('name', 'Unknown')} — {score:.0f}/100 | {label} | {action} {' | '.join(flags)}")
            rows.append(f"  {item.get('reason', '')}")
            evidence = item.get("evidence", [])
            if evidence:
                rows.append("  Evidence: " + "; ".join(str(x) for x in evidence[:3]))
        for part in chunk_text("\n".join(rows)):
            await update.message.reply_text(part)
    except AgentError as exc:
        await update.message.reply_text(f"မလုပ်ဆောင်နိုင်သေးပါ။ {exc}")


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
            BotCommand("post", "Telegram post ရေးရန်"),
            BotCommand("curate", "Content curation"),
            BotCommand("scout", "Group scouting"),
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
    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("curate", curate_command))
    application.add_handler(CommandHandler("scout", scout_command))
    application.add_handler(CommandHandler("targets", targets_command))
    application.add_handler(CommandHandler("forward", forward_command))
    application.add_error_handler(error_handler)
    logger.info("Starting Telegram Content Strategist bot with model %s", LLM_MODEL)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
