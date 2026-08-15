"""Safe Telegram group-scouting helpers.

This module evaluates only user-supplied group metadata. It deliberately does not
perform network reconnaissance, port scanning, DNS enumeration, scraping, or
attempts to discover private Telegram groups.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any


MAX_GROUPS = max(1, int(os.getenv("GROUPSCAN_MAX_GROUPS", "50")))
MAX_FILE_BYTES = max(10_000, int(os.getenv("GROUPSCAN_MAX_FILE_BYTES", "1000000")))


class GroupScanInputError(ValueError):
    """Raised when a supplied group list cannot be parsed safely."""


def parse_member_count(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
    if not match:
        return None
    value = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _clean_group(item: dict[str, Any]) -> dict[str, Any] | None:
    name = str(item.get("name", item.get("group_name", ""))).strip()
    if not name:
        return None
    description = str(item.get("description", item.get("bio", ""))).strip()
    raw_members = item.get("member_count", item.get("members", item.get("member_count_text")))
    return {
        "name": name[:300],
        "description": description[:2000],
        "member_count": parse_member_count(raw_members),
    }


def _from_json(raw: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        parsed = parsed.get("groups", parsed.get("data", []))
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        if isinstance(item, dict):
            cleaned = _clean_group(item)
            if cleaned:
                result.append(cleaned)
    return result


def _from_delimited(raw: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return result

    # Pipe/tab input is the most readable Telegram format:
    # name | description | member count
    if any("|" in line or "\t" in line for line in lines):
        for line in lines:
            parts = [part.strip() for part in re.split(r"\s*\|\s*|\t+", line)]
            if len(parts) < 2:
                continue
            member_count = parse_member_count(parts[-1]) if len(parts) >= 3 else None
            description = " | ".join(parts[1:-1]) if len(parts) >= 3 else parts[1]
            cleaned = _clean_group(
                {"name": parts[0], "description": description, "member_count": member_count}
            )
            if cleaned:
                result.append(cleaned)
        return result

    # CSV input with an optional header. Accepted columns are name/group_name,
    # description/bio, and member_count/members.
    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except csv.Error:
        rows = []
    if rows and any("name" in row or "group_name" in row for row in rows):
        for row in rows:
            cleaned = _clean_group(row)
            if cleaned:
                result.append(cleaned)
        return result

    # Conservative fallback for plain lines: name only, with unknown metadata.
    for line in lines:
        cleaned = _clean_group({"name": line})
        if cleaned:
            result.append(cleaned)
    return result


def parse_groups(raw: str) -> list[dict[str, Any]]:
    """Parse at most MAX_GROUPS group records from user-supplied text."""
    if not isinstance(raw, str):
        raise GroupScanInputError("Group list must be text")
    raw = raw.strip()
    if not raw:
        return []
    if len(raw.encode("utf-8")) > MAX_FILE_BYTES:
        raise GroupScanInputError("Group list is larger than the configured input limit")

    groups = _from_json(raw) if raw.startswith(("[", "{")) else []
    if not groups:
        groups = _from_delimited(raw)
    # Deduplicate by case-folded name while preserving order.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        key = group["name"].casefold()
        if key not in seen:
            unique.append(group)
            seen.add(key)
    return unique[:MAX_GROUPS]


def split_niche_and_groups(raw: str) -> tuple[str, str]:
    """Treat a first plain line as the niche, leaving the remaining text as groups."""
    lines = raw.strip().splitlines()
    if not lines:
        return "", ""
    first = lines[0].strip()
    if "|" not in first and "\t" not in first and not first.startswith(("[", "{")):
        return first, "\n".join(lines[1:]).strip()
    return "", raw.strip()


def render_report(result: dict[str, Any]) -> str:
    """Render a compact mobile-friendly English report from structured model output."""
    items = result.get("groups", [])
    counts = {"target": 0, "review": 0, "exclude": 0, "spam": 0}
    rows = ["🔎 GroupScan Report", ""]
    for item in items:
        action = str(item.get("action", "review")).lower()
        if action not in {"target", "review", "exclude"}:
            action = "review"
        counts[action] += 1
        if item.get("spam_flag"):
            counts["spam"] += 1
        try:
            score = max(0, min(100, float(item.get("fit_score", 0))))
        except (TypeError, ValueError):
            score = 0
        flags = []
        if item.get("spam_flag"):
            flags.append("SPAM FLAG")
        if not item.get("match"):
            flags.append("IRRELEVANT")
        suffix = " | " + " | ".join(flags) if flags else ""
        rows.append(
            f"• {item.get('name', 'Unknown')} — {score:.0f}/100 | "
            f"{str(item.get('quality_label', 'unknown')).upper()} | {action.upper()}{suffix}"
        )
        rows.append(f"  {item.get('reason', 'No reason was provided.')}")
        evidence = item.get("evidence", [])
        if evidence:
            rows.append("  Evidence: " + "; ".join(str(x) for x in evidence[:3]))
    summary = (
        f"Total {len(items)} | Target {counts['target']} | "
        f"Review {counts['review']} | Exclude {counts['exclude']} | Spam flags {counts['spam']}"
    )
    return "\n".join(rows + ["", summary])
