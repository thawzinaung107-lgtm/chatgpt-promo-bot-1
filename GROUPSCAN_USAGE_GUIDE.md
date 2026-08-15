# Telegram GroupScan Usage Guide

## 1. What GroupScan does

`GroupScan` evaluates Telegram groups against a target niche using only the group metadata you provide: **group name, description, and member count**. It returns a recommendation of `TARGET`, `REVIEW`, or `EXCLUDE`, together with a relevance score, spam flags, and supporting evidence.

> GroupScan does not automatically discover groups, join groups, send messages, or measure real engagement. It evaluates the supplied metadata only.

## 2. Requirements

The bot must be running and connected to an AI provider. If you use the in-bot provider pool, an administrator can configure a provider from a private chat:

```text
/provider_add
/provider_test <provider_name>
/provider_use <provider_name>
```

Use `/provider_list` to confirm the active profile. If you use environment-based configuration instead, set `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL`.

If GroupScan should be restricted to specific chats, configure `GROUPSCAN_ALLOWED_CHAT_IDS`. Use the following command inside a group to see its chat ID:

```text
/id
```

## 3. Main commands

| Command | Example | Purpose |
|---|---|---|
| `/groupscan <niche>` | `/groupscan AI tools` | Evaluate a supplied group list against a niche. |
| `/scout <niche>` | `/scout digital marketing` | Backward-compatible alias for `/groupscan`. |
| `/id` | `/id` | Show the current Telegram chat ID. |
| `/help` | `/help` | Show the bot command list. |

`/groupscan` is the recommended command. `/scout` remains available for backward compatibility.

## 4. Scan inline text

Put the target niche on the first line and one group record per line after it. Separate each record with pipe characters:

```text
/groupscan AI tools
AI Myanmar | AI tools and productivity discussion | 12K
Marketing MM | Digital marketing and advertising | 850
Crypto Deals | Token giveaways and instant profit | 45K
```

The record format is:

```text
Group Name | Group Description | Member Count
```

The parser accepts member counts such as `12K`, `1.5M`, `850`, and `12,000`. You may leave the member count blank when it is unavailable, but the result may be marked `UNKNOWN` or `REVIEW` because the evidence is incomplete.

## 5. Scan a replied message

You can reply to a message containing a group list and use the command:

```text
/groupscan AI tools
```

If you reply to a group-list message without specifying a niche, use `/groupscan`. Providing a clear niche is recommended because it produces a more useful relevance score.

## 6. Scan an uploaded TXT, CSV, or JSON file

For a larger list, upload a UTF-8 `.txt`, `.csv`, or `.json` file to the bot. Reply to the uploaded file with:

```text
/groupscan AI tools
```

### CSV format

CSV input should include a header row. The accepted logical fields are `name` or `group_name`, `description` or `bio`, and `members` or `member_count`.

```csv
name,description,members
AI Myanmar,AI tools discussion,12K
Marketing MM,Digital marketing and advertising,850
```

### JSON format

You may provide an object containing a `groups` array:

```json
{
  "groups": [
    {
      "name": "AI Myanmar",
      "description": "AI tools discussion",
      "member_count": "12K"
    },
    {
      "name": "Marketing MM",
      "description": "Digital marketing and advertising",
      "member_count": 850
    }
  ]
}
```

A direct JSON array is also accepted:

```json
[
  {"name": "AI Myanmar", "description": "AI tools", "member_count": "12K"}
]
```

## 7. Read the GroupScan report

The report contains the following fields:

| Field | Meaning |
|---|---|
| `0–100 score` | Estimated niche relevance based only on the supplied metadata. It is not a popularity or engagement score. |
| `TARGET` | The supplied evidence suggests that the group may be suitable for the target niche. |
| `REVIEW` | The information is incomplete or ambiguous and requires human review. |
| `EXCLUDE` | The group appears irrelevant or contains negative signals in the supplied metadata. |
| `SPAM FLAG` | The name or description contains a spam-like signal. |
| `IRRELEVANT` | The supplied information does not match the target niche. |
| `HIGH / MEDIUM / LOW / UNKNOWN` | The quality label inferred from the supplied evidence. `UNKNOWN` means there is not enough evidence. |
| `Evidence` | The supplied input that supports the model’s reason or action. |

Example output:

```text
• AI Myanmar — 90/100 | HIGH | TARGET
  The description directly matches the target niche.
  Evidence: AI tools; productivity discussion

• Crypto Deals — 10/100 | LOW | EXCLUDE | SPAM FLAG
  The description contains instant-profit and giveaway signals.
  Evidence: instant profit; giveaways
```

A `TARGET` result is only a recommendation. The bot does not automatically save the group to the target list, join it, or send promotional messages. Review the evidence and make the final decision manually.

## 8. Recommended workflow

First define a specific niche, such as `AI tools for content creators` or `digital marketing for Myanmar businesses`. Then collect group names, descriptions, and member counts from a reliable source and normalize them into one input format. Run GroupScan and review the evidence behind each `TARGET`, `REVIEW`, and `EXCLUDE` result. Treat `REVIEW` as a human-verification queue and avoid using groups marked `EXCLUDE` or `SPAM FLAG`.

A large member count does not prove that a group has good engagement or a high-quality audience. GroupScan should be used as a **pre-filter**, not as a substitute for human audience-quality review.

## 9. Common errors

| Error | Resolution |
|---|---|
| `No group records were found` | Use `Group Name | Description | Member Count`, a valid CSV header, or valid JSON with a `name` field. |
| `Invalid GroupScan input` | Confirm that the text uses pipe-delimited, CSV, or JSON syntax and that uploaded files are UTF-8. |
| `This chat is not included in the GroupScan allowlist` | Use `/id` to get the chat ID and add it to `GROUPSCAN_ALLOWED_CHAT_IDS`. |
| `The language model request failed` | Run `/provider_test <name>` and verify the API key, endpoint, model ID, and response-format settings. |
| `GroupScan result did not contain one matching result per supplied group` | The model returned an incomplete result. Split a large scan into smaller batches and try again. |
| Many groups are marked `REVIEW` | The descriptions may be too vague or the target niche may not be specific enough. |

## 10. Limits and known boundaries

By default, one scan accepts up to `50` groups and a UTF-8 input file up to `1,000,000` bytes. Configure `GROUPSCAN_MAX_GROUPS` and `GROUPSCAN_MAX_FILE_BYTES` to change these limits.

GroupScan cannot verify real member activity, recent posts, engagement rates, administrator quality, privacy status, or current membership. It uses only the metadata supplied by the user and is designed not to invent unsupported facts.

---

**Summary:** Define a clear niche, prepare consistent group metadata, run `/groupscan <niche>`, and manually review the evidence behind each `TARGET`, `REVIEW`, and `EXCLUDE` recommendation.
