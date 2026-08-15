# Telegram AI Content Strategist & Growth Manager

An English-first Telegram AI agent for content creation, smart curation, protected forwarding, and Telegram group scouting. The bot is designed around the supplied identity: it writes engaging mobile-friendly posts, adds context before forwarding, and evaluates candidate groups using only their supplied name, description, and member count.

## Safety boundary

This repository now contains only the content-strategy bot. The previous unrelated promo-scanning and token-handling logic has been removed. The bot does not request, store, decode, validate, or transmit access tokens or private account credentials.

The model is instructed not to invent facts, news, statistics, engagement, or group characteristics. Smart forwarding is disabled unless the target is explicitly present in `TARGETS_JSON`, the requester is listed in `ADMIN_IDS`, the content is text/caption-readable, and the model returns a confident relevance decision with an English context intro.

## Features

| Workflow | Command | Behavior |
|---|---|---|
| General AI agent | `/agent <request>` | Uses the configured Telegram strategist identity to answer content, campaign, curation, and growth requests in English by default. |
| Content creation | `/post <source>` | Converts source material into an English Telegram post with a hook, short sections, restrained emojis, and a CTA. |
| Content curation | `/curate <content>` | Classifies the content and drafts a 1–2 sentence context intro without sending anything. |
| GroupScan scouting | `/groupscan <niche>` or `/scout <niche>` | Scores niche fit, flags spam or irrelevance, and returns `target`, `review`, or `exclude`. Accepts pipe-delimited text, CSV, JSON, or a replied UTF-8 text file. |
| Chat ID helper | `/id` | Shows the current chat ID so an administrator can configure the GroupScan chat allowlist. |
| Smart forwarding | Reply to a source message, then `/forward <target_chat_id>` | Checks relevance first, sends the generated intro, and then forwards the original message only for approved targets. |
| Target review | `/targets` | Shows the configured target allowlist to admins. |
| Provider pool | `/provider_list`, `/provider_add`, `/provider_use`, `/provider_test`, `/provider_remove` | Lets admins add and select API key, endpoint, model, and compatibility settings from a private Telegram chat. |

## Configuration

Copy `.env.example` to `.env` for local development, or add the same values as environment variables in the hosting dashboard. The bot loads `.env` automatically through `python-dotenv`. Never commit real credentials.

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Token created through [@BotFather](https://t.me/BotFather). |
| `LLM_API_KEY` | No | Optional direct API fallback. You can leave it blank and add providers from Telegram. Local alias `OPENAI_API_KEY` is also accepted. |
| `LLM_BASE_URL` | No | Compatible API root, usually ending in `/v1`; full `/chat/completions` URLs are normalized. Local alias `OPENAI_API_BASE` is also accepted. |
| `LLM_MODEL` | No | Provider model ID. Local alias `OPENAI_MODEL` is also accepted. |
| `LLM_RESPONSE_FORMAT` | No | `auto`, `json_schema`, `json_object`, or `none`; defaults to `auto` and falls back for providers without strict schema support. |
| `LLM_MAX_TOKENS_PARAM` | No | `auto`, `max_tokens`, or `max_completion_tokens`; defaults to model-aware `auto`. |
| `LLM_TIMEOUT_SECONDS` | No | Request timeout; defaults to `60`. |
| `LLM_MAX_RETRIES` | No | SDK retry count; defaults to `2`. |
| `LLM_REASONING_EFFORT` | No | Optional GPT-5 reasoning effort: `minimal`, `low`, `medium`, or `high`. |
| `ADMIN_IDS` | Recommended | Comma-separated Telegram numeric user IDs allowed to use admin commands, including provider management. |
| `TARGETS_JSON` | Required for forwarding | JSON object or array containing `chat_id`, `label`, `description`, and optional `allowed_categories`. |
| `MAX_INPUT_CHARS` | No | Maximum prompt size; defaults to `8000`. |
| `GROUPSCAN_ALLOWED_CHAT_IDS` | No | Comma-separated chat IDs where `/groupscan` is allowed. Leave blank to allow any chat. |
| `GROUPSCAN_MAX_GROUPS` | No | Maximum groups accepted per scan; defaults to `50`. |
| `GROUPSCAN_MAX_FILE_BYTES` | No | Maximum UTF-8 input-file size; defaults to `1000000` bytes. |
| `DATABASE_URL` | No | Database URL; defaults to `sqlite:///bot.db`. Use a PostgreSQL URL in production when desired. |
| `API_KEY_ENCRYPTION_KEY` | No | Fernet key used to encrypt API keys in the database. If blank, a stable key is derived from `BOT_TOKEN`; preserve it after deployment. |
| `PROVIDER_STORE_KEY` | No | Legacy alias accepted for `API_KEY_ENCRYPTION_KEY`. |
| `LOG_LEVEL` | No | Python logging level; defaults to `INFO`. |

Example `TARGETS_JSON`:

```json
[
  {
    "chat_id": -1001234567890,
    "label": "AI Myanmar",
    "description": "AI tools and productivity content for Myanmar audiences",
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

For a generic OpenAI-compatible provider, configure the connection like this:

```dotenv
LLM_API_KEY=your_provider_key
LLM_BASE_URL=https://provider.example.com/v1
LLM_MODEL=provider_model_name
LLM_RESPONSE_FORMAT=auto
LLM_MAX_TOKENS_PARAM=auto
```

The bot first attempts strict JSON Schema output, then falls back to JSON mode and finally plain JSON parsing when the provider does not support the stronger format. The model, API key, and base URL are never sent to Telegram users.

### Database-backed provider profiles and preferences

The bot stores provider profiles and user preferences in the database configured by `DATABASE_URL`. SQLite is the default for local use; PostgreSQL is supported for production deployments. API keys are encrypted with Fernet before they are written to the database. Set `API_KEY_ENCRYPTION_KEY` explicitly in production and preserve it permanently; if it is blank, the bot derives a stable key from `BOT_TOKEN`.

Add the administrator’s Telegram user ID to `ADMIN_IDS`, start the bot, and open a private chat with it. Use `/provider_add` and complete the guided flow for the profile name, API key, endpoint, model, and advanced compatibility options. The bot attempts to delete the plaintext API-key message immediately; if deletion fails, it aborts the setup rather than saving the key.

Use `/provider_list` to see masked keys and redacted endpoints, `/provider_use <name>` to select the active profile, `/provider_test [name]` to verify a profile, and `/provider_remove <name>` to delete one. Profiles are isolated by Telegram user ID, so one user cannot activate or remove another user’s profile. The selected profile is used automatically by `/agent`, `/post`, `/curate`, `/groupscan`, and `/forward`.

Use `/preferences` to view saved preferences and `/prefs_set <key> <value>` to save them. Supported keys are `language`, `default_niche`, and `style`. The current English build accepts `language=English`; `default_niche` is used when `/groupscan` is called without an explicit niche.

If you are upgrading from the older encrypted `provider_pool.enc` format, stop the bot and run the one-time migration utility. Use the Telegram user ID that owned the old global provider pool:

```bash
python migrate_provider_pool.py \
  --legacy provider_pool.enc \
  --database sqlite:///bot.db \
  --user-id 123456789
```

The migration imports profiles without printing API keys. After verifying the database-backed profiles with `/provider_list`, keep the database and encryption key backed up securely.

Example GroupScan input:

```text
/groupscan AI tools
AI Myanmar | AI tools discussion for Myanmar audiences | 12K
Marketing MM | Digital marketing and advertising | 850
Crypto Deals | token giveaways and instant profit | 45K
```

The same command accepts JSON:

```json
{"groups":[{"name":"AI Myanmar","description":"AI tools","member_count":"12K"}]}
```

For a file workflow, upload a UTF-8 `.txt`, `.csv`, or `.json` file, reply to it with `/groupscan AI tools`, and the bot will parse the metadata. Use `/id` in a group to see its chat ID, then set `GROUPSCAN_ALLOWED_CHAT_IDS` if scanning should be limited to specific chats. See the dedicated English [GroupScan Usage Guide](GROUPSCAN_USAGE_GUIDE.md) for step-by-step examples and troubleshooting.

The bot treats member count as supplied context only. It does not claim that a group is active or high quality merely because it has more members.

## Deployment

The included `render.yaml` starts the polling process from `bot.py`. Add the environment variables from the configuration table to the service settings, then deploy the repository. For local deployments, `DATABASE_URL=sqlite:///bot.db` creates a persistent SQLite database. For production, use PostgreSQL or attach persistent storage for the SQLite file, preserve `API_KEY_ENCRYPTION_KEY`, and back up the database securely. For production use, choose hosting that keeps a Telegram polling process continuously available, and grant the bot only the channel/group permissions it needs.

## Implementation notes

The bot uses structured JSON responses for the general agent, post creation, curation, and GroupScan. Every GroupScan result must contain exactly one matching result per supplied group; malformed or incomplete results are rejected. The integration intentionally does not execute the attached network-reconnaissance code, perform host/port scans, enumerate subdomains, probe SNI/VPN handshakes, or search for CDN origin IPs. Model calls run in a worker thread so the asynchronous Telegram update loop remains responsive. Database schema creation is idempotent and runs at startup.

## References

1. [Telegram Bot API documentation](https://core.telegram.org/bots/api)
2. [Telegram BotFather](https://t.me/BotFather)
3. [Render documentation](https://render.com/docs)
4. [OpenAI API documentation](https://platform.openai.com/docs/overview)
