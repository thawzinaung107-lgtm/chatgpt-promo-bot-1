import asyncio
import os
from types import SimpleNamespace

os.environ["TARGETS_JSON"] = '[{"chat_id": -1001, "label": "AI MM", "description": "AI", "allowed_categories": ["AI"]}]'
os.environ["LLM_API_KEY"] = "test-key"

import bot
from bot import TARGETS, ContentAgent, normalize_base_url, parse_groups, parse_member_count
from groupscan import parse_groups as parse_group_records, render_report, split_niche_and_groups

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
assert normalize_base_url("https://provider.example/v1/") == "https://provider.example/v1"


class FakeCompletions:
    def create(self, **kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"
        schema_name = kwargs["response_format"]["json_schema"]["name"]
        if schema_name == "telegram_group_scout":
            content = '{"groups": [{"name": "AI Myanmar", "fit_score": 90, "match": true, "spam_flag": false, "quality_label": "high", "reason": "Relevant", "evidence": ["AI tools"], "action": "target"}]}'
        elif schema_name == "telegram_agent_answer":
            content = '{"answer":"မြန်မာလို အကြံပြုချက်","mode":"campaign_planning","source_facts":["source"],"needs_review":false}'
        else:
            content = '{"post":"Hook\\n\\nCTA","category":"AI","cta":"CTA","source_facts":["fact"],"needs_review":false}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=FakeCompletions())


bot.OpenAI = FakeClient
agent = ContentAgent()
assert agent._json_text("```json\n{\"ok\":true}\n```") == '{"ok":true}'
os.environ["LLM_MAX_TOKENS_PARAM"] = "max_tokens"
assert agent._token_parameter(100) == {"max_tokens": 100}
previous_model = bot.LLM_MODEL
bot.LLM_MODEL = "openai/gpt-5-mini"
os.environ["LLM_MAX_TOKENS_PARAM"] = "auto"
assert agent._token_parameter(100) == {"max_completion_tokens": 100}
bot.LLM_MODEL = previous_model
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
