# Telegram AI Content Strategist Bot

## End-to-End User Manual and Administrator Guide

**Repository:** `thawzinaung107-lgtm/chatgpt-promo-bot-1`  
**Audience:** Bot users, content operators, channel administrators, and deployment administrators  
**Language:** English by default  
**Runtime:** Python Telegram polling bot with an OpenAI-compatible API client and SQLite/PostgreSQL persistence

> This manual describes the implementation currently in the repository. It covers the complete lifecycle from deployment and provider setup through content generation, review, publishing, scheduling, GroupScan, media posts, and maintenance.

## 1. What the Bot Does

The bot is an English-first Telegram Content Strategist and Growth Manager. It can turn source material into mobile-friendly Telegram posts, answer free-form content and growth questions, classify content before forwarding, evaluate user-supplied group metadata, manage drafts, publish to allowlisted targets, schedule one-time or recurring posts, attach media, and preserve provider profiles and preferences in a database.

The AI uses only the facts supplied in the request or source material. It must not invent news, statistics, engagement levels, audience characteristics, or group quality. GroupScan evaluates only the supplied group name, description, and member count. The bot does not perform host scanning, port scanning, DNS enumeration, private-group discovery, credential scanning, or token extraction.

## 2. Quick Start for Operators

A normal operator workflow is:

| Step | Action | Command or operation |
|---:|---|---|
| 1 | Ask the agent for a strategy answer or content idea. | `/agent <request>` or send a private-chat message without a command |
| 2 | Generate a post from source material. | `/post <source>` |
| 3 | Review the saved draft. | `/draft_list`, `/preview <draft_id>` |
| 4 | Edit if necessary. | `/draft_edit <draft_id> <new text>` |
| 5 | Approve only after checking the source. | `/approve <draft_id>` |
| 6 | Publish manually or schedule it. | `/publish ...` or `/schedule ...` |
| 7 | Check delivery history. | `/audit` |

Publishing commands require an administrator account and a destination that exists in `TARGETS_JSON`. The bot must also have permission to send messages in the destination channel or group.

## 3. Roles and Permissions

The bot uses the Telegram user ID from the incoming message. Provider profiles, drafts, templates, channel profiles, preferences, and audit records are scoped to that user ID. One user cannot read or modify another user’s database records through the bot.

| Capability | Regular user | Administrator in `ADMIN_IDS` |
|---|---:|---:|
| `/start`, `/help`, `/agent`, private free-form AI chat | Yes | Yes |
| `/post`, draft review, template management, preferences | Yes | Yes |
| GroupScan and `/scout` | Yes, subject to chat allowlist | Yes, subject to chat allowlist |
| `/preferences`, `/prefs_set`, `/channel_list`, `/template_list`, `/repeat_list`, `/audit` | Own records | Own records |
| Add, select, test, or remove API providers | No | Yes, in private chat |
| `/targets` | No | Yes |
| `/forward` | No | Yes |
| `/publish`, `/schedule`, `/repeat`, `/repeat_remove` | No | Yes |
| `/publish_multi`, `/batch_publish` | No | Yes |
| `/channel_add`, `/channel_remove` | No | Yes |

If `ADMIN_IDS` is empty or incorrect, administrator-only commands will be rejected. Telegram numeric user IDs are not usernames; use a trusted Telegram ID utility or the bot’s `/id` command to identify a chat ID, and configure administrator user IDs separately.

## 4. Deployment and Configuration

### 4.1 Required environment variables

At minimum, configure `BOT_TOKEN`. The AI provider can be configured directly through environment variables, or an administrator can add a provider profile from inside Telegram. The database defaults to local SQLite.

```dotenv
BOT_TOKEN=your_telegram_bot_token
ADMIN_IDS=123456789
DATABASE_URL=sqlite:///bot.db
API_KEY_ENCRYPTION_KEY=your_fernet_key
```

For production PostgreSQL, use a database URL such as:

```dotenv
DATABASE_URL=postgresql://username:password@host:5432/database_name
```

The complete configuration is:

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | None | Telegram Bot API token from BotFather. Required. |
| `ADMIN_IDS` | Empty | Comma-separated Telegram user IDs allowed to run admin commands. |
| `DATABASE_URL` | `sqlite:///bot.db` | SQLite or PostgreSQL persistence URL. |
| `API_KEY_ENCRYPTION_KEY` | Derived from `BOT_TOKEN` if blank | Fernet key used to encrypt API keys before database storage. |
| `PROVIDER_STORE_KEY` | None | Legacy alias for `API_KEY_ENCRYPTION_KEY`. |
| `LLM_API_KEY` | Empty | Optional direct OpenAI-compatible API key. `OPENAI_API_KEY` is also accepted. |
| `LLM_BASE_URL` | Empty | Optional OpenAI-compatible API root, normally ending in `/v1`. `OPENAI_API_BASE` is also accepted. |
| `LLM_MODEL` | `gpt-5-mini` | Direct-fallback model ID. `OPENAI_MODEL` is also accepted. |
| `LLM_RESPONSE_FORMAT` | `auto` | `auto`, `json_schema`, `json_object`, or `none`. |
| `LLM_MAX_TOKENS_PARAM` | `auto` | `auto`, `max_tokens`, or `max_completion_tokens`. |
| `LLM_TIMEOUT_SECONDS` | `60` | LLM request timeout. |
| `LLM_MAX_RETRIES` | `2` | LLM SDK retry count. |
| `LLM_REASONING_EFFORT` | Empty | Optional GPT-5 effort: `minimal`, `low`, `medium`, or `high`. |
| `TARGETS_JSON` | Empty | Allowlisted publishing and forwarding destinations. |
| `MAX_INPUT_CHARS` | `8000` | Maximum model prompt input size. |
| `GROUPSCAN_ALLOWED_CHAT_IDS` | Empty | Optional comma-separated chats permitted to use GroupScan. |
| `GROUPSCAN_MAX_GROUPS` | `50` | Maximum group records accepted per scan. |
| `GROUPSCAN_MAX_FILE_BYTES` | `1000000` | Maximum UTF-8 group-list size in bytes. |
| `PUBLISH_MAX_RETRIES` | `2` | Additional Telegram delivery attempts after the first failure; capped at five. |
| `PUBLISH_RETRY_DELAY_SECONDS` | `5` | Base delay between delivery retries. |
| `MAX_MEDIA_FILES` | `10` | Maximum media files attached to a draft. |
| `MAX_BUTTONS` | `20` | Maximum URL buttons attached to a draft. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

### 4.2 Target allowlist configuration

Publishing and forwarding use the same explicit destination allowlist. Keep `TARGETS_JSON` on one line in a hosting dashboard:

```dotenv
TARGETS_JSON=[{"chat_id":-1001234567890,"label":"AI Myanmar","description":"AI tools and productivity content for Myanmar audiences","allowed_categories":["AI","productivity"]}]
```

Each target should include a numeric `chat_id`, a human-readable `label`, a `description`, and optionally `allowed_categories`. The bot does not bypass Telegram permissions; add the bot to each destination and grant only the rights it needs.

### 4.3 Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with real values
python bot.py
```

For hosted deployment, configure the same variables in the hosting dashboard. The polling process must stay continuously online for scheduled and recurring publishing to run. PostgreSQL is recommended for production; SQLite is suitable for local use or a deployment with persistent disk storage.

### 4.4 Encryption and backup requirements

API keys are encrypted with Fernet before they are written to the database. In production, set `API_KEY_ENCRYPTION_KEY` explicitly and preserve it permanently. If it is blank, the bot derives a stable encryption key from `BOT_TOKEN`; changing the bot token can make previously stored keys unreadable.

Back up the database and the encryption key together. A database backup without the encryption key is not sufficient to recover stored API credentials. Never commit `.env`, `bot.db`, PostgreSQL dumps containing secrets, or the encryption key to Git.

## 5. First-Time Administrator Setup

Complete these steps before operating publishing workflows:

1. Create the bot with [BotFather](https://t.me/BotFather) and copy the token into `BOT_TOKEN`.
2. Identify the administrator’s Telegram user ID and add it to `ADMIN_IDS`.
3. Configure `DATABASE_URL` and `API_KEY_ENCRYPTION_KEY`.
4. Configure `TARGETS_JSON` for every channel or group where the bot may publish or forward.
5. Add the bot to each target and grant send-message permission. For forwarding, ensure the bot can access the source message and forward it.
6. Start the bot and open a private chat with it.
7. Add an API provider using `/provider_add`, or use the direct environment fallback.
8. Verify the connection with `/provider_test`.
9. Use `/targets` to confirm the destination allowlist.
10. Create a test draft, approve it, and publish it to a test channel before using production targets.

## 6. Complete Command Reference

### 6.1 General and help commands

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/start` | Everyone | `/start` | Displays the bot identity and command overview. |
| `/help` | Everyone | `/help` | Displays the command help text. |
| `/agent` | Everyone | `/agent <request>` | Sends a free-form strategy, content, curation, campaign, or growth request to the AI agent. |
| Private free-form message | Private chat | Any text without `/` | Runs the same general agent workflow without requiring `/agent`. Group chats remain command-driven. |
| `/id` | Everyone | `/id` | Shows the current Telegram chat ID. This is useful for configuring `GROUPSCAN_ALLOWED_CHAT_IDS`, not for discovering a user ID. |

The agent returns English by default. It preserves the safety rule that unsupported facts, statistics, or audience claims must be marked as unknown or needing review.

### 6.2 Content creation and drafts

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/post` | Everyone | `/post <source>` or reply to a message with `/post` | Generates a hook-led Telegram post and saves it as a database draft when persistence is available. |
| `/draft_list` | Everyone | `/draft_list` or `/draft_list <status>` | Lists the requesting user’s drafts. Optional statuses include `draft`, `approved`, `scheduled`, `publishing`, `published`, and `failed`. |
| `/preview` | Everyone | `/preview <draft_id>` | Shows the draft, category, status, schedule, and source. |
| `/draft_edit` | Everyone | `/draft_edit <draft_id> <new post text>` | Replaces the post text and returns the draft to `DRAFT` status. |
| `/approve` | Everyone | `/approve <draft_id>` | Moves a verified draft to `APPROVED`. Drafts marked `needs_review` cannot be approved. |

When `/post` receives incomplete or ambiguous source material, the draft may be marked for review. Editing the text does not remove the `needs_review` flag; regenerate from a corrected source with `/post` if approval remains blocked.

### 6.3 Publishing and scheduling

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/publish` | Admin only | `/publish <draft_id> <target_chat_id>` | Publishes one approved draft to one allowlisted target. |
| `/schedule` | Admin only | `/schedule <draft_id> <target_chat_id> <UTC ISO-8601 time>` | Schedules one approved draft for a future UTC time. |
| `/publish_multi` | Admin only | `/publish_multi <draft_id> <chat_id_1,chat_id_2>` | Sends one approved draft to several allowlisted destinations and returns a batch ID. |
| `/batch_publish` | Admin only | `/batch_publish <draft_id_1,draft_id_2> <target_chat_id>` | Sends several approved drafts to one allowlisted target. |
| `/audit` | Everyone | `/audit` | Lists the requesting user’s recent delivery attempts, statuses, target IDs, attempt numbers, and batch IDs. |

Example:

```text
/post New AI tools are now available for small businesses.
/draft_list
/preview 12
/approve 12
/publish 12 -1001234567890
```

For a one-time schedule:

```text
/schedule 12 -1001234567890 2026-08-20T09:00:00Z
```

The bot checks due drafts approximately every 30 seconds while the polling process is online. It claims a due job before sending it, records the Telegram message ID on success, and marks failures in the database. Delivery retries follow `PUBLISH_MAX_RETRIES` and `PUBLISH_RETRY_DELAY_SECONDS`.

### 6.4 Channel profiles and signatures

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/channel_add` | Admin only | `/channel_add <chat_id> <IANA_timezone> <label> \|\| <signature>` | Saves or updates a channel profile with timezone and optional signature. |
| `/channel_list` | Everyone | `/channel_list` | Lists the requesting user’s channel profiles. |
| `/channel_remove` | Admin only | `/channel_remove <chat_id>` | Removes the requesting admin’s channel profile. |

Example:

```text
/channel_add -1001234567890 Asia/Yangon AI Myanmar || Join us: @ai_mm
/channel_list
```

The signature is appended to text and media posts sent to that target when the profile belongs to the current user. IANA timezone names such as `Asia/Yangon`, `Asia/Singapore`, `Europe/London`, and `UTC` are accepted.

### 6.5 Recurring posts

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/repeat` | Admin only | `/repeat <draft_id> <target_chat_id> <interval_minutes> <start_time> [until_time]` | Creates a recurring schedule for an approved or already published draft. Minimum interval: 60 minutes. |
| `/repeat_list` | Everyone | `/repeat_list` | Lists the requesting user’s recurring schedules. |
| `/repeat_remove` | Admin only | `/repeat_remove <recurring_id>` | Deletes the requesting admin’s recurring schedule. |

Example using the channel profile timezone:

```text
/repeat 12 -1001234567890 1440 2026-08-20T09:00:00
```

Example with an explicit end time:

```text
/repeat 12 -1001234567890 10080 2026-08-20T09:00:00 2026-12-31T23:59:00
```

A start or end time without an offset uses the target channel profile timezone; if no profile exists, UTC is used. Times with `Z` or an explicit offset are converted to UTC. The worker claims recurring jobs before delivery, calculates the next run after success, and retries a failed delivery after approximately five minutes unless the end time has passed.

### 6.6 Templates

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/template_add` | Everyone | `/template_add <name> \|\| <body> \|\| <category> \|\| <CTA>` | Saves or updates a user-scoped template. Only the name and body are required. |
| `/template_list` | Everyone | `/template_list` | Lists the requesting user’s templates. |
| `/template_post` | Everyone | `/template_post <template_name> <source text>` | Applies a template structure while generating a new draft. |
| `/template_remove` | Everyone | `/template_remove <name>` | Removes the requesting user’s template. |

Example:

```text
/template_add announcement || Hook: {{source}} || announcement || Read more
/template_post announcement New AI tools released today
/preview 13
```

Templates are instructions for structure and tone; the AI still applies the source-evidence and no-invention rules.

### 6.7 Rich media, buttons, previews, and slideshows

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/media_attach` | Everyone | Reply to a photo/video/image document, then `/media_attach <draft_id>` | Attaches Telegram media file IDs to a draft. |
| `/slideshow_add` | Everyone | Reply to a photo/image document, then `/slideshow_add <draft_id>` | Adds a photo to a multi-photo album draft. Repeat for additional photos. |
| `/media_clear` | Everyone | `/media_clear <draft_id>` | Removes all media from a draft. |
| `/buttons_set` | Everyone | `/buttons_set <draft_id> Label \| URL \|\| Label 2 \| URL 2` | Adds one-row-per-button inline URL buttons. Accepts `https://`, `http://`, and `tg://` URLs. |
| `/preview_set` | Everyone | `/preview_set <draft_id> on` or `off` | Enables or disables link previews for text posts. |
| `/watermark_set` | Everyone | `/watermark_set <draft_id> <text>` | Stores image watermark text to apply before publishing. |

A single photo or video is sent as a media post. Multiple photos are sent as a Telegram media group. Media file IDs are persisted in the database, allowing scheduled and recurring publishing without re-uploading the files.

Image watermarks are rendered when the image is published. Video watermarking is intentionally rejected because the current build does not modify video bytes. Rich media controls are fully supported by single-target publish, scheduled publishing, and recurring publishing. Multi-channel and batch workflows are primarily text delivery workflows and should be tested with a non-production target before using media-heavy campaigns.

### 6.8 Content curation and smart forwarding

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/curate` | Everyone | `/curate <content>` or reply to a message with `/curate` | Classifies content and drafts a short context intro without sending anything. |
| `/targets` | Admin only | `/targets` | Lists configured allowlisted destinations and their categories. |
| `/forward` | Admin only | Reply to a source message, then `/forward <target_chat_id>` | Checks relevance, sends an AI-written context intro, and forwards the original only when the decision is confident and approved. |

Forwarding requires a text or caption-readable source message. If relevance is uncertain, the source is missing text, the target is not allowlisted, or the model requests review, the original message is not forwarded.

### 6.9 GroupScan

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/groupscan` | Everyone, subject to chat allowlist | `/groupscan <niche>` followed by records, or reply to a group-list file | Parses and evaluates user-supplied group metadata. |
| `/scout` | Everyone, subject to chat allowlist | Same as `/groupscan` | Backward-compatible alias for `/groupscan`. |

The simplest format is:

```text
/groupscan AI tools
AI Myanmar | AI tools discussion for Myanmar audiences | 12K
Marketing MM | Digital marketing and advertising | 850
Crypto Deals | token giveaways and instant profit | 45K
```

The first plain line is treated as the niche when it does not contain a delimiter. Records can use pipe or tab delimiters:

```text
Group Name | Description | Member Count
AI Myanmar | AI tools | 12K
Marketing MM | Digital marketing | 850
```

CSV is supported with an optional header. Accepted field aliases include `name` or `group_name`, `description` or `bio`, and `member_count`, `members`, or `member_count_text`.

JSON can be an array or an object containing `groups` or `data`:

```json
{
  "groups": [
    {"name": "AI Myanmar", "description": "AI tools", "member_count": "12K"},
    {"name": "Marketing MM", "description": "Digital marketing", "members": 850}
  ]
}
```

For a file workflow, upload a UTF-8 `.txt`, `.csv`, or `.json` file, reply to that file, and use `/groupscan AI tools`. The parser removes blank lines and comment lines beginning with `#`, normalizes member counts such as `12K`, `1.5M`, and comma-formatted numbers, removes duplicate names case-insensitively, and processes at most `GROUPSCAN_MAX_GROUPS` records. The configured input-size limit is `GROUPSCAN_MAX_FILE_BYTES`.

The report uses these labels:

| Report element | Meaning |
|---|---|
| `TARGET` | The supplied evidence indicates a strong niche fit suitable for targeting. |
| `REVIEW` | The evidence is incomplete or ambiguous; inspect the group manually. |
| `EXCLUDE` | The supplied evidence indicates irrelevance or an unsuitable fit. |
| `SPAM FLAG` | The supplied name or description contains an obvious spam signal. |
| `IRRELEVANT` | The model judged the group unrelated to the requested niche. |
| `HIGH`, `MEDIUM`, `LOW`, `UNKNOWN` | Quality label based only on supplied evidence, never an invented engagement estimate. |

A score is clamped to `0–100` and represents niche fit, not popularity. The summary includes total groups, target count, review count, exclude count, and spam-flag count. GroupScan does not browse Telegram, discover private groups, inspect real engagement, or infer quality from member count alone.

### 6.10 Provider profiles and API management

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/provider_add` | Admin only, private chat | `/provider_add` | Starts a guided provider setup conversation. |
| `/provider_list` | Admin only | `/provider_list` | Shows the requesting admin’s provider profiles with masked keys and redacted endpoints. |
| `/provider_use` | Admin only | `/provider_use <profile_name>` | Selects the requesting admin’s active provider. |
| `/provider_test` | Admin only | `/provider_test` or `/provider_test <profile_name>` | Tests the active or named provider with a short structured AI request. |
| `/provider_remove` | Admin only | `/provider_remove <profile_name>` | Removes the requesting admin’s provider profile. |
| `/cancel` | During `/provider_add` only | `/cancel` | Aborts the guided provider setup. |

The `/provider_add` conversation asks for:

1. A profile name using letters, numbers, `_`, `-`, or `.` and no more than 40 characters.
2. The API key. The bot attempts to delete the key message immediately; if deletion fails, it stops without saving the key.
3. The endpoint, such as `https://api.openai.com/v1`. Enter `-` to use the direct environment default.
4. The exact model ID.
5. Advanced options in this format:

```text
response_format | tokens_param | timeout_seconds | max_retries | reasoning
```

A safe default is:

```text
auto | auto | 60 | 2 |
```

Supported response formats are `auto`, `json_schema`, `json_object`, and `none`. Supported token modes are `auto`, `max_tokens`, and `max_completion_tokens`. Timeout values must be between 10 and 600 seconds, and provider retry counts must be between 0 and 10. Supported reasoning values are empty, `minimal`, `low`, `medium`, and `high`.

The selected provider is used by `/agent`, `/post`, `/curate`, `/groupscan`, and `/forward`. The API client tries strict JSON Schema first, then JSON mode, then tolerant plain JSON parsing for providers with weaker compatibility.

### 6.11 Preferences

| Command | Who can use it | Syntax | Behavior |
|---|---|---|---|
| `/preferences` | Everyone | `/preferences` | Shows the requesting user’s saved preferences. |
| `/prefs_set` | Everyone | `/prefs_set <language\|default_niche\|style> <value>` | Saves a user-scoped preference. |

The current build is English-first, so `language=English` is supported. `default_niche` is used when `/groupscan` is called without an explicit niche. `style` is included in the AI user-preference context and can influence writing style without overriding safety rules.

## 7. Draft and Publishing State Model

Drafts are persisted in the database and belong to the Telegram user who created them.

| State | Meaning | Typical next action |
|---|---|---|
| `DRAFT` | Generated or edited but not approved. | Review, edit, or approve. |
| `APPROVED` | Verified and ready for administrator publishing. | Publish, schedule, or create a recurring schedule. |
| `SCHEDULED` | Waiting for its one-time publish time. | Leave the bot process online. |
| `PUBLISHING` | Claimed by the background worker and being delivered. | Wait for completion or inspect logs. |
| `PUBLISHED` | Successfully sent; Telegram message ID is stored. | Reuse for recurring posts or create a new draft. |
| `FAILED` | Delivery failed or a batch was partially unsuccessful. | Inspect `/audit`, correct the issue, and approve again if needed. |

A recurring schedule has its own active state and points to a draft. It can continue publishing an approved or previously published draft at the configured interval.

## 8. Database and Upgrade Operations

The database schema is created automatically at startup and is safe to initialize repeatedly. It includes provider profiles, user preferences, drafts, channel profiles, templates, recurring schedules, and audit logs. Rich-media draft columns are added idempotently for existing databases.

### 8.1 SQLite operations

For local development:

```dotenv
DATABASE_URL=sqlite:///bot.db
```

Keep `bot.db` on persistent storage. Do not place it in a temporary directory in production.

### 8.2 PostgreSQL operations

For production:

```dotenv
DATABASE_URL=postgresql://username:password@host:5432/database_name
```

Use a managed PostgreSQL backup policy and restrict database access to the bot service. Preserve `API_KEY_ENCRYPTION_KEY` across redeployments.

### 8.3 Legacy provider-pool migration

Older versions used an encrypted `provider_pool.enc` file. Stop the bot, preserve a backup of the old file, and run:

```bash
python migrate_provider_pool.py \
  --legacy provider_pool.enc \
  --database sqlite:///bot.db \
  --user-id 123456789
```

The migration imports the old profiles without printing API keys. Verify the result with `/provider_list`, then keep the database and encryption key backed up securely.

## 9. Troubleshooting

### The bot exits with `BOT_TOKEN is required`

Set `BOT_TOKEN` to the token created by BotFather. Do not include surrounding quotation marks unless your hosting platform requires them.

### The agent says no API key is configured

Either set `LLM_API_KEY` and the corresponding endpoint/model variables, or add a provider profile with `/provider_add` from an administrator’s private chat. Use `/provider_test` after selecting the profile.

### Provider setup stops after API-key entry

The bot could not delete the plaintext API-key message. Retry in a private chat where the bot has permission to delete messages. The profile is not saved when deletion fails.

### `/provider_list` or preferences are unavailable

Check `DATABASE_URL`, `API_KEY_ENCRYPTION_KEY`, database connectivity, and file permissions for SQLite. If an explicit encryption key is used, confirm that it is a valid Fernet key and has not changed.

### Publishing says the target is not in the allowlist

Add the destination to `TARGETS_JSON`, restart the bot so the configuration reloads, and verify it with `/targets`. Use the numeric chat ID, not only a channel username.

### Publishing fails even though the target is allowlisted

Confirm that the bot is a member of the target and has permission to send messages. For forwarding, verify that the source message is accessible and contains text or a caption. Review `/audit` and the service logs for the Telegram error.

### A scheduled post does not run

The polling process must remain online. Confirm that the scheduled time is in the future, includes a valid UTC offset or `Z`, the target is allowlisted, and the database is persistent. The worker checks approximately every 30 seconds, so delivery is not guaranteed to occur at the exact second.

### A recurring post does not run

Confirm that the recurring record appears in `/repeat_list`, its target remains in `TARGETS_JSON`, the draft is still available, and the bot process is online. A failed delivery is retried after approximately five minutes unless the end time has passed.

### GroupScan rejects the file

Use a UTF-8 text file, keep it within `GROUPSCAN_MAX_FILE_BYTES`, and provide a supported JSON, CSV, pipe-delimited, tab-delimited, or plain-line format. The parser requires a group name; descriptions and member counts may be unknown.

### GroupScan is blocked in a chat

If `GROUPSCAN_ALLOWED_CHAT_IDS` is set, the current chat ID must be included. Use `/id` in the chat, add the ID to the environment variable, and restart the bot.

### Media publishing fails

Confirm that the replied message contains a Telegram photo, video, or image document. A video draft can contain only one video, and a video cannot use image watermarking. Check that the draft has not exceeded `MAX_MEDIA_FILES` and that the bot can download and resend the media.

### Buttons are rejected

Use `Label | URL` for each button and separate buttons with `||`. URLs must begin with `https://`, `http://`, or `tg://`. Keep the total within `MAX_BUTTONS`.

## 10. Security and Operating Rules

Never paste API keys into public groups. Use `/provider_add` only in a private administrator chat, and verify that the plaintext key message was deleted. Never commit `.env`, database files, encryption keys, provider-pool files, or PostgreSQL dumps to Git.

Keep `ADMIN_IDS` as small as practical. Keep `TARGETS_JSON` limited to destinations where the bot is genuinely authorized to publish. Use test channels before production channels. Review AI-generated drafts and source evidence before approval, especially when `needs_review` is true.

The bot is a content and publishing assistant. It does not verify real-world group engagement, does not discover private Telegram groups, does not scrape Telegram, and does not perform network reconnaissance. It must not be used to infer facts that were not supplied by the operator.

## 11. Operational Checklist

Before production use, confirm that the bot token is valid, administrator IDs are correct, the database and encryption key are backed up, provider connectivity passes, targets are allowlisted, the bot has the correct Telegram permissions, a test draft can be approved and published, scheduled publishing has been observed in a test channel, and `/audit` shows delivery records.

After deployment, monitor service logs, review failed audit records, rotate provider keys through `/provider_remove` and `/provider_add` when required, back up the database, and preserve the encryption key during every redeployment.

## References

1. [Telegram Bot API documentation](https://core.telegram.org/bots/api)
2. [Telegram BotFather](https://t.me/BotFather)
3. [OpenAI API documentation](https://platform.openai.com/docs/overview)
4. [Render documentation](https://render.com/docs)
5. [Project README](README.md)
6. [GroupScan Usage Guide](GROUPSCAN_USAGE_GUIDE.md)
7. [Project repository](https://github.com/thawzinaung107-lgtm/chatgpt-promo-bot-1)

## Change History

This manual corresponds to the Phase 1–4 publishing implementation currently tracked in the repository. When command behavior or environment variables change, update this manual and the README together.

---

**Document owner:** Manus AI  
**Recommended review cadence:** Whenever a command, database schema, provider configuration, or deployment manifest changes
