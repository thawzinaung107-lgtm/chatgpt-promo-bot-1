import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ["TARGETS_JSON"] = '[{"chat_id": -1001, "label": "AI MM", "description": "AI", "allowed_categories": ["AI"]}]'
os.environ["LLM_API_KEY"] = "test-key"

import bot
from bot import SYSTEM_IDENTITY, TARGETS, ContentAgent, normalize_base_url, parse_groups, parse_member_count
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
