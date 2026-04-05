"""SDK builder — spawn a Claude Code SDK agent to set up protocol tooling.

When a new enrichment protocol requires software that isn't already in the
tool registry (a new model, a CLI tool, a custom script), this module spawns
a Claude Code SDK agent that can install, configure, and verify whatever is
needed. The agent's output streams inline to the terminal, and the final
result is returned to the calling authoring session as a verified
command_template + a list of new tool registry entries to add.

Expected result JSON from the SDK agent:
{
    "command_template": "...",   // shell command using {file_path}, {archive_root}, {sidecar_path}
    "new_tools": [               // entries to add to config/tools.yaml
        {
            "name": "...",
            "type": "ollama_model|system_binary|python_package|custom_script",
            "identifier": "...",
            "notes": "..."
        }
    ],
    "verification_output": "...",  // what the agent ran to confirm it works
    "notes": "..."                 // any extra context for the protocol instructions field
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SDK_SEPARATOR = "─" * 50

_AGENT_SYSTEM_PROMPT = """\
You are a tooling setup agent for Sheaf, a personal media archive system.

Your job is to install and verify whatever external tooling a new enrichment protocol needs, \
then return a verified command_template the protocol can use.

## Contract

When you are done, output a single JSON block (and NOTHING else after it) of this form:

```json
{
    "command_template": "<shell command using {file_path}, {archive_root}, {sidecar_path}>",
    "new_tools": [
        {
            "name": "<short identifier>",
            "type": "<ollama_model|system_binary|python_package|custom_script>",
            "identifier": "<model tag, binary path, package name, or script path>",
            "notes": "<one-line description>"
        }
    ],
    "verification_output": "<what you ran and what it returned to confirm everything works>",
    "notes": "<any extra context the protocol's instructions field should include>"
}
```

## Rules

- command_template must print valid JSON to stdout. Variables: {file_path}, {archive_root}, {sidecar_path}.
- Verify the tool actually works by running it against a test input before returning.
- If you write a custom script, save it under the sheaf project scripts/ directory.
- Prefer tools that are already available on the system before installing new ones.
- If installing an ollama model, confirm ollama is running first (`ollama list`).
- Keep the command_template self-contained — don't rely on the calling shell's environment beyond PATH.
"""


def run_sdk_builder(
    task: str,
    media_context: str,
    tool_registry_summary: str,
    project_dir: Path,
) -> dict[str, Any]:
    """Spawn a Claude Code SDK agent to set up protocol tooling.

    Streams the agent's output inline to stdout with a visual separator.
    Returns the parsed result dict on success, raises RuntimeError on failure.
    """
    try:
        return asyncio.run(_run_async(task, media_context, tool_registry_summary, project_dir))
    except KeyboardInterrupt:
        raise RuntimeError("SDK agent interrupted by user")


async def _run_async(
    task: str,
    media_context: str,
    tool_registry_summary: str,
    project_dir: Path,
) -> dict[str, Any]:
    from claude_code_sdk import query, ClaudeCodeOptions

    prompt = _build_prompt(task, media_context, tool_registry_summary, project_dir)

    options = ClaudeCodeOptions(
        system_prompt=_AGENT_SYSTEM_PROMPT,
        allowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        permission_mode="acceptEdits",
        cwd=str(project_dir),
        max_turns=30,
    )

    print(f"\n{_SDK_SEPARATOR} SDK agent {_SDK_SEPARATOR}")

    full_text = ""
    result_message = None

    try:
        async for message in query(prompt=prompt, options=options):
            from claude_code_sdk.types import AssistantMessage, ResultMessage, ToolUseBlock, TextBlock

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        # Print incrementally
                        print(block.text, end="", flush=True)
                        full_text += block.text
                    elif isinstance(block, ToolUseBlock):
                        # Show tool calls inline
                        _print_tool_call(block)

            elif isinstance(message, ResultMessage):
                result_message = message
                if message.result:
                    full_text += message.result

    finally:
        print(f"\n{_SDK_SEPARATOR * 2 + _SDK_SEPARATOR[: len('─ SDK agent ─')]}")
        print()

    if result_message and result_message.subtype != "success":
        raise RuntimeError(f"SDK agent ended with: {result_message.subtype}")

    return _extract_result(full_text)


def _build_prompt(
    task: str,
    media_context: str,
    tool_registry_summary: str,
    project_dir: Path,
) -> str:
    scripts_dir = project_dir / "scripts"
    return (
        f"## Task\n{task}\n\n"
        f"## Media context\n{media_context}\n\n"
        f"## Current tool registry\n{tool_registry_summary}\n\n"
        f"## Project directory\n{project_dir}\n"
        f"## Scripts directory (for custom scripts)\n{scripts_dir}\n\n"
        "Install and verify whatever tooling this requires, then return the result JSON."
    )


def _print_tool_call(block: Any) -> None:
    """Print a compact representation of a tool call."""
    name = getattr(block, "name", "?")
    inp = getattr(block, "input", {})
    if name == "Bash":
        cmd = inp.get("command", "")
        # Truncate long commands
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        print(f"\n  Bash: {cmd}", flush=True)
    elif name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", inp.get("path", ""))
        print(f"\n  {name}: {path}", flush=True)
    else:
        print(f"\n  {name}(...)", flush=True)


def _extract_result(text: str) -> dict[str, Any]:
    """Extract the JSON result block from the agent's output."""
    # Try to find a ```json ... ``` block first
    import re
    fence_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        raw = fence_match.group(1)
    else:
        # Fall back: find the last { ... } block
        last_brace = text.rfind("{")
        if last_brace == -1:
            raise RuntimeError("SDK agent did not return a JSON result block")
        raw = text[last_brace:]
        # Find matching closing brace
        depth = 0
        end = last_brace
        for i, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    raw = raw[: i + 1]
                    break

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SDK agent returned malformed JSON: {e}\n\nRaw: {raw[:500]}")

    # Validate required fields
    if "command_template" not in result:
        raise RuntimeError("SDK agent result missing 'command_template'")

    result.setdefault("new_tools", [])
    result.setdefault("verification_output", "")
    result.setdefault("notes", "")

    return result
