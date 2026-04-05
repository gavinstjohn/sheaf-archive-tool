"""Tools registry — read/write config/tools.yaml.

Tracks external tools, models, and scripts that have been installed to support
enrichment protocols. Consulted during protocol authoring so the model knows
what's already available before deciding what new tooling is needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Valid tool types
TOOL_TYPES = frozenset({"ollama_model", "system_binary", "python_package", "custom_script"})


def load_tools(registry_path: Path) -> list[dict[str, Any]]:
    """Load the tool registry. Returns an empty list if the file doesn't exist yet."""
    if not registry_path.exists():
        return []
    with open(registry_path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("tools", [])


def save_tools(registry_path: Path, tools: list[dict[str, Any]]) -> None:
    """Persist the full tools list back to the registry file."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# config/tools.yaml\n"
        "# Auto-managed by Sheaf — tracks external tools, models, and scripts installed for protocols.\n"
        "# Do not edit manually. Use `sheaf protocols new` or `sheaf protocols edit` to manage.\n\n"
    )
    body = yaml.dump({"tools": tools}, default_flow_style=False, sort_keys=False, allow_unicode=True)
    registry_path.write_text(header + body)
    log.debug("Saved %d tool(s) to %s", len(tools), registry_path)


def add_tools(registry_path: Path, new_entries: list[dict[str, Any]], installed_by: str) -> None:
    """Append new tool entries to the registry, stamping installed_by and verified_at.

    Skips entries whose name+type already exist (no duplicates).
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = load_tools(registry_path)
    existing_keys = {(t.get("name"), t.get("type")) for t in existing}

    added = 0
    for entry in new_entries:
        key = (entry.get("name"), entry.get("type"))
        if key in existing_keys:
            log.debug("Tool already registered: %s (%s) — skipping", *key)
            continue
        entry = dict(entry)
        entry.setdefault("installed_by", installed_by)
        entry.setdefault("verified_at", now)
        existing.append(entry)
        existing_keys.add(key)
        added += 1

    if added:
        save_tools(registry_path, existing)
        log.info("Registered %d new tool(s) from protocol %r", added, installed_by)


def format_registry_for_prompt(tools: list[dict[str, Any]]) -> str:
    """Return a compact, human-readable summary of the tool registry for use in prompts."""
    if not tools:
        return "Tool registry: (empty — no tools installed yet)"

    lines = [f"Tool registry ({len(tools)} tool(s) available):"]
    for t in tools:
        name = t.get("name", "?")
        ttype = t.get("type", "?")
        identifier = t.get("identifier") or t.get("path") or ""
        notes = t.get("notes", "")
        installed_by = t.get("installed_by", "")
        line = f"  • {name} [{ttype}]"
        if identifier:
            line += f"  —  {identifier}"
        if notes:
            line += f"  ({notes})"
        if installed_by:
            line += f"  [used by: {installed_by}]"
        lines.append(line)
    return "\n".join(lines)
