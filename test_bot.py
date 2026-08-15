import asyncio
import io
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from telegram.error import TelegramError

os.environ["TARGETS_JSON"] = '[{"chat_id": -1001, "label": "AI MM", "description": "AI", "allowed_categories": ["AI"]}]'
os.environ["LLM_API_KEY"] = "test-key"

import bot
from bot import SYSTEM_IDENTITY, TARGETS, ContentAgent, _send_text_with_retries, add_watermark, normalize_base_url, parse_buttons, parse_groups, parse_local_datetime, parse_member_count, parse_utc_datetime, validate_timezone
from groupscan import parse_groups as parse_group_records, render_report, split_niche_and_groups
from migrate_provider_pool import migrate
from provider_pool import ProviderPoolStore, ProviderProfile

assert "Output English by default" in SYSTEM_IDENTITY
assert "Output " + "Burmese" not in SYSTEM_IDENTITY
assert parse_member_count("12K") == 12000
assert parse_member_count("1.5M") == 1500000
assert parse_member_count("850") == 850
assert parse_member_count("unknown") is None

rows = parse_groups("AI Myanmar | AI tools discussion | 12K\nMarketing MM | Digital marketing | 850")
assert rows == [
    {"name": "AI Myanmar", "description": "AI tools discussion", "member_count": 12000},
    {"name": "Marketing MM", "description": "Digital marketing", "member_count": 850},
]

json_rows = parse_group_records('[{"name":"AI MM","description":"AI","member_count":"2K"}]')
assert json_rows[0]["member_count"] == 2000
csv_rows = parse_group_records("name,description,members\nAI CSV,AI tools,1.2K")
assert csv_rows == [{"name": "AI CSV", "description": "AI tools", "member_count": 1200}]
niche, group_text = split_niche_and_groups("AI tools\nAI Myanmar | AI tools | 12K")
assert niche == "AI tools" and "AI Myanmar" in group_text
assert "Target 1" in render_report({"groups": [{"name": "AI Myanmar", "fit_score": 90, "match": True, "spam_flag": False, "quality_label": "high", "reason": "Relevant", "evidence": ["AI"], "action": "target"}]})
assert TARGETS[0]["chat_id"] == -1001
assert normalize_base_url("https://provider.example/v1/chat/completions") == "https://provider.example/v1"

with tempfile.TemporaryDirectory() as tmp:
    pool_path = Path(tmp) / "bot.db"
    store_key = Fernet.generate_key().decode()
    store = ProviderPoolStore(f"sqlite:///{pool_path}", store_key)
    profile = ProviderProfile(
        name="test-provider",
        api_key="secret-provider-key-1234",
        base_url="https://provider.example/v1?api_key=do-not-show",
        model="openai/gpt-5-mini",
    )
    owner_id = 111
    other_user_id = 222
    store.upsert(owner_id, profile)
    assert store.list_profiles(owner_id)[0]["active"] is True
    assert store.list_profiles(other_user_id) == []
    listing = store.list_profiles(owner_id)[0]
    assert listing["api_key"] != profile.api_key
    assert "do-not-show" not in listing["base_url"]
    reloaded = ProviderPoolStore(f"sqlite:///{pool_path}", store_key)
    assert reloaded.get(owner_id, "test-provider").api_key == profile.api_key
    reloaded.activate(owner_id, "test-provider")
    reloaded.set_preference(owner_id, "default_niche", "AI tools")
    assert reloaded.get_preference(owner_id, "default_niche") == "AI tools"
    reloaded.remove(owner_id, "test-provider")
    assert reloaded.list_profiles(owner_id) == []

    legacy_path = Path(tmp) / "provider_pool.enc"
    legacy_key = Fernet.generate_key().decode()
    database_key = Fernet.generate_key().decode()
    legacy_payload = {"active": "legacy-provider", "providers": {"legacy-provider": profile.__dict__}}
    legacy_path.write_bytes(Fernet(legacy_key.encode()).encrypt(json.dumps(legacy_payload).encode("utf-8")))
    imported_db = Path(tmp) / "imported.db"
    assert migrate(str(legacy_path), f"sqlite:///{imported_db}", owner_id, legacy_key, database_key) == 1
    imported = ProviderPoolStore(f"sqlite:///{imported_db}", database_key)
    assert imported.get(owner_id, "legacy-provider").api_key == profile.api_key
assert normalize_base_url("https://provider.example/v1/") == "https://provider.example/v1"
assert parse_utc_datetime("2026-08-20T09:00:00Z").tzinfo is not None
assert parse_utc_datetime("2026-08-20T09:00:00+06:30").utcoffset() == timedelta(0)

with tempfile.TemporaryDirectory() as tmp:
    draft_store = ProviderPoolStore(f"sqlite:///{Path(tmp) / 'drafts.db'}", Fernet.generate_key().decode())
    draft_result = {
        "post": "Hook\n\nBody\n\nCTA",
        "category": "AI",
        "cta": "Read more",
        "source_facts": ["fact"],
        "needs_review": False,
    }
    draft = draft_store.create_draft(1001, "Source material", draft_result)
    assert draft["status"] == "draft"
    media_draft = draft_store.create_draft(1001, "Media source", draft_result, {"media_type": "photo", "media_file_ids": ["file-1", "file-2"], "buttons": [{"label": "Read", "url": "https://example.com"}], "link_preview_disabled": True, "watermark_text": "AI Myanmar"})
    assert media_draft["media_type"] == "photo" and len(media_draft["media_file_ids"]) == 2
    assert media_draft["buttons"][0]["label"] == "Read" and media_draft["link_preview_disabled"] is True
    updated_media = draft_store.update_draft_media(1001, media_draft["id"], buttons=[{"label": "Join", "url": "https://example.com/join"}], watermark_text="Updated")
    assert updated_media["buttons"][0]["label"] == "Join" and updated_media["watermark_text"] == "Updated"
    assert draft_store.get_draft(1002, draft["id"]) is None
    approved = draft_store.update_draft(1001, draft["id"], status="approved")
    assert approved["status"] == "approved"
    scheduled = draft_store.update_draft(
        1001,
        draft["id"],
        status="scheduled",
        channel_chat_id=-1001,
        scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert scheduled["status"] == "scheduled"
    claimed = draft_store.claim_due_drafts()
    assert len(claimed) == 1 and claimed[0]["status"] == "publishing"
    published = draft_store.update_draft(
        1001,
        draft["id"],
        status="published",
        published_at=datetime.now(timezone.utc),
        published_message_id=77,
    )
    assert published["status"] == "published" and published["published_message_id"] == 77
    channel = draft_store.upsert_channel_profile(1001, -1001, "AI Myanmar", "Asia/Yangon", "Join us: @ai_mm")
    assert channel["timezone"] == "Asia/Yangon" and channel["signature"] == "Join us: @ai_mm"
    assert draft_store.get_channel_profile(1002, -1001) is None
    template = draft_store.upsert_template(1001, "announcement", "Hook: {{source}}", "announcement", "Read more")
    assert draft_store.get_template(1001, "announcement")["body"] == template["body"]
    assert draft_store.get_template(1002, "announcement") is None
    recurring = draft_store.create_recurring(1001, draft["id"], -1001, 1440, datetime.now(timezone.utc) - timedelta(minutes=1))
    claimed_recurring = draft_store.claim_due_recurring()
    assert len(claimed_recurring) == 1 and claimed_recurring[0]["active"] is False
    next_run = datetime.now(timezone.utc) + timedelta(days=1)
    completed_recurring = draft_store.complete_recurring(recurring["id"], next_run, datetime.now(timezone.utc))
    assert completed_recurring["active"] is True

assert validate_timezone("Asia/Yangon") == "Asia/Yangon"
assert parse_local_datetime("2026-08-20T09:00:00", "Asia/Yangon").hour == 2
assert parse_buttons("Read | https://example.com || Join | tg://user?id=1")[1]["label"] == "Join"
try:
    parse_buttons("Invalid button")
except ValueError:
    pass
else:
    raise AssertionError("Invalid button syntax should fail")
from PIL import Image
image_buffer = io.BytesIO()
Image.new("RGB", (120, 80), "blue").save(image_buffer, format="PNG")
assert len(add_watermark(image_buffer.getvalue(), "Watermark")) > 0


class RetryBot:
    def __init__(self):
        self.calls = 0

    async def send_message(self, chat_id, text):
        self.calls += 1
        if self.calls == 1:
            raise TelegramError("temporary failure")
        return SimpleNamespace(message_id=1234)


with tempfile.TemporaryDirectory() as tmp:
    audit_store = ProviderPoolStore(f"sqlite:///{Path(tmp) / 'audit.db'}", Fernet.generate_key().decode())
    retry_bot = RetryBot()
    old_retry_count = bot.PUBLISH_MAX_RETRIES
    old_retry_delay = bot.PUBLISH_RETRY_DELAY_SECONDS
    bot.PUBLISH_MAX_RETRIES = 1
    bot.PUBLISH_RETRY_DELAY_SECONDS = 0
    sent = asyncio.run(_send_text_with_retries(retry_bot, audit_store, 1001, "hello", -1001, "batch_publish", 1, "batch123"))
    bot.PUBLISH_MAX_RETRIES = old_retry_count
    bot.PUBLISH_RETRY_DELAY_SECONDS = old_retry_delay
    assert sent.message_id == 1234 and retry_bot.calls == 2
    logs = audit_store.list_audit_logs(1001)
    assert any(row["status"] == "failed" and row["batch_id"] == "batch123" for row in logs)
    assert audit_store.list_audit_logs(1002) == []


class FakeCompletions:
    def create(self, **kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"
        schema_name = kwargs["response_format"]["json_schema"]["name"]
        if schema_name == "telegram_group_scout":
            content = '{"groups": [{"name": "AI Myanmar", "fit_score": 90, "match": true, "spam_flag": false, "quality_label": "high", "reason": "Relevant", "evidence": ["AI tools"], "action": "target"}]}'
        elif schema_name == "telegram_agent_answer":
            content = '{"answer":"English strategy suggestion","mode":"campaign_planning","source_facts":["source"],"needs_review":false}'
        else:
            content = '{"post":"Hook\\n\\nCTA","category":"AI","cta":"CTA","source_facts":["fact"],"needs_review":false}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=FakeCompletions())


bot.OpenAI = FakeClient
with tempfile.TemporaryDirectory() as tmp:
    scoped_store = ProviderPoolStore(f"sqlite:///{Path(tmp) / 'scoped.db'}", Fernet.generate_key().decode())
    scoped_profile = ProviderProfile(name="scoped", api_key="scoped-key-1234", base_url="", model="scoped-model")
    scoped_store.upsert(999, scoped_profile)
    scoped_store.set_preference(999, "default_niche", "creator tools")
    previous_pool = bot.PROVIDER_POOL
    bot.PROVIDER_POOL = scoped_store
    scoped_agent = ContentAgent(user_id=999)
    assert scoped_agent.provider_name == "scoped"
    assert scoped_agent.model == "scoped-model"
    assert scoped_agent.default_niche == "creator tools"
    bot.PROVIDER_POOL = previous_pool
agent = ContentAgent()
assert agent._json_text("```json\n{\"ok\":true}\n```") == '{"ok":true}'
agent.max_tokens_param = "max_tokens"
assert agent._token_parameter(100) == {"max_tokens": 100}
previous_model = agent.model
agent.model = "openai/gpt-5-mini"
agent.max_tokens_param = "auto"
assert agent._token_parameter(100) == {"max_completion_tokens": 100}
agent.model = previous_model
result = asyncio.run(agent.create_post("AI tools are provided in the source."))
assert result["needs_review"] is False
assert result["category"] == "AI"
scout_result = asyncio.run(agent.scout([{"name": "AI Myanmar", "description": "AI tools", "member_count": 12000}], "AI"))
assert scout_result["groups"][0]["action"] == "target"
agent_result = asyncio.run(agent.agent("Plan a content calendar."))
assert agent_result["mode"] == "campaign_planning"


class FallbackCompletions:
    def create(self, **kwargs):
        if kwargs.get("response_format", {}).get("type") == "json_schema":
            raise RuntimeError("strict schema unsupported")
        assert kwargs["response_format"]["type"] == "json_object"
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='```json\\n{"answer":"fallback","mode":"curation","source_facts":[],"needs_review":false}\\n```'))])


class FallbackClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=FallbackCompletions())


bot.OpenAI = FallbackClient
os.environ["LLM_RESPONSE_FORMAT"] = "auto"
fallback_agent = ContentAgent()
fallback_result = asyncio.run(fallback_agent.agent("Fallback test"))
assert fallback_result["answer"] == "fallback"
print("unit helpers, provider compatibility, fallback, GroupScan, and agent responses: ok")
