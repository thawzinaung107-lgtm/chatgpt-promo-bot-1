# Telegram AI Content Strategist & Growth Manager

A Burmese-first Telegram AI agent for content creation, smart curation, protected forwarding, and Telegram group scouting. The bot is designed around the supplied identity: it writes engaging mobile-friendly posts, adds context before forwarding, and evaluates candidate groups using only their supplied name, description, and member count.

## Safety boundary

This repository now contains only the content-strategy bot. The previous unrelated promo-scanning and token-handling logic has been removed. The bot does not request, store, decode, validate, or transmit access tokens or private account credentials.

The model is instructed not to invent facts, news, statistics, engagement, or group characteristics. Smart forwarding is disabled unless the target is explicitly present in `TARGETS_JSON`, the requester is listed in `ADMIN_IDS`, the content is text/caption-readable, and the model returns a confident relevance decision with a Burmese context intro.

## Features

| Workflow | Command | Behavior |
|---|---|---|
| Content creation | `/post <source>` | Converts source material into a Burmese Telegram post with hook, short sections, restrained emojis, and CTA. |
| Content curation | `/curate <content>` | Classifies the content and drafts a 1–2 sentence context intro without sending anything. |
| Group scouting | `/scout <niche>` followed by one group per line | Scores niche fit, flags spam or irrelevance, and returns `target`, `review`, or `exclude`. |
| Smart forwarding | Reply to a source message, then `/forward <target_chat_id>` | Checks relevance first, sends the generated intro, and then forwards the original message only for approved targets. |
| Target review | `/targets` | Shows the configured target allowlist to admins. |

## Configuration

Copy `.env.example` to `.env` for local development, or add the same values as environment variables in the hosting dashboard. Never commit real credentials.

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Token created through [@BotFather](https://t.me/BotFather). |
| `LLM_API_KEY` | Yes | API key for the configured OpenAI-compatible language-model endpoint. |
| `LLM_BASE_URL` | No | Optional compatible API base URL. Leave blank for the provider default. |
| `LLM_MODEL` | No | Model ID; defaults to `gpt-5-mini`. |
| `ADMIN_IDS` | Recommended | Comma-separated Telegram numeric user IDs allowed to use `/forward` and `/targets`. |
| `TARGETS_JSON` | Required for forwarding | JSON object or array containing `chat_id`, `label`, `description`, and optional `allowed_categories`. |
| `MAX_INPUT_CHARS` | No | Maximum prompt size; defaults to `8000`. |
| `LOG_LEVEL` | No | Python logging level; defaults to `INFO`. |

Example `TARGETS_JSON`:

```json
[
  {
    "chat_id": -1001234567890,
    "label": "AI Myanmar",
    "description": "မြန်မာဘာသာ AI tools နှင့် productivity ဆိုင်ရာ content များ",
    "allowed_categories": ["AI", "productivity"]
  }
]
```

When entering this value into a hosting dashboard, keep it on one line. A channel or group must also permit the bot to send messages and forward content; the code does not bypass Telegram permissions.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with real values
python bot.py
```

Example group input:

```text
/scout AI tools
AI Myanmar | မြန်မာဘာသာ AI tool များဆွေးနွေးခြင်း | 12K
Marketing MM | Digital marketing နှင့် ads | 850
Crypto Deals | token giveaways and instant profit | 45K
```

The bot treats member count as supplied context only. It does not claim that a group is active or high quality merely because it has more members.

## Deployment

The included `render.yaml` starts the polling process from `bot.py`. Add the environment variables from the configuration table to the service settings, then deploy the repository. For production use, choose hosting that keeps a Telegram polling process continuously available, and grant the bot only the channel/group permissions it needs.

## Implementation notes

The bot uses structured JSON responses for post creation, curation, and group scouting. This keeps the forwarding decision separate from the user-facing text and makes it possible to reject malformed or incomplete model responses. Model calls run in a worker thread so the asynchronous Telegram update loop remains responsive.

## References

1. [Telegram Bot API documentation](https://core.telegram.org/bots/api)
2. [Telegram BotFather](https://t.me/BotFather)
3. [Render documentation](https://render.com/docs)
4. [OpenAI API documentation](https://platform.openai.com/docs/overview)
