from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from ..exceptions import AdapterError
from .base import (
    AdapterCapabilities,
    AdapterResponse,
    BaseAdapter,
    Message,
    ToolCall,
    ToolDefinition,
)

log = logging.getLogger(__name__)

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

# Statuses that warrant a retry
_RETRY_STATUSES = {429, 529}
_MAX_RETRIES = 3
_BASE_BACKOFF = 2.0   # seconds
_MAX_BACKOFF = 60.0


class ClaudeAdapter(BaseAdapter):

    def __init__(self, model: str = "claude-opus-4-6", api_key_env: str = "ANTHROPIC_API_KEY") -> None:
        self.model = model
        self._api_key_env = api_key_env
        self._capabilities = AdapterCapabilities(
            vision=True,
            tool_use=True,
            max_context=200_000,
            streaming=False,
        )

    @property
    def capabilities(self) -> AdapterCapabilities:
        return self._capabilities

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8096,
    ) -> AdapterResponse:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise AdapterError(
                f"API key not found. Set the {self._api_key_env} environment variable."
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [_format_message(m) for m in messages],
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [_format_tool(t) for t in tools]

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            _API_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
            method="POST",
        )

        raw = self._request_with_retry(req)
        return _parse_response(raw)

    def _request_with_retry(self, req: urllib.request.Request) -> dict:
        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                status = e.code
                try:
                    body = json.loads(e.read())
                    msg = body.get("error", {}).get("message", str(e))
                except Exception:
                    msg = str(e)

                if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    wait = min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)
                    log.warning("API returned %d (%s), retrying in %.0fs (attempt %d/%d)",
                                status, msg, wait, attempt + 1, _MAX_RETRIES)
                    time.sleep(wait)
                    last_error = AdapterError(f"HTTP {status}: {msg}")
                    continue
                raise AdapterError(f"HTTP {status}: {msg}") from e
            except urllib.error.URLError as e:
                raise AdapterError(f"Network error: {e.reason}") from e

        raise AdapterError("Max retries exceeded") from last_error


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def _format_message(msg: Message) -> dict:
    """Convert internal Message to Anthropic API format."""
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}
    # Already a list of content blocks (e.g. tool_result blocks)
    return {"role": msg.role, "content": msg.content}


def _format_tool(t: ToolDefinition) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "input_schema": t.input_schema,
    }


def _parse_response(raw: dict) -> AdapterResponse:
    """Parse an Anthropic API response into an AdapterResponse."""
    content_blocks: list[dict] = raw.get("content", [])
    stop_reason: str = raw.get("stop_reason", "end_turn")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(ToolCall(
                id=block["id"],
                name=block["name"],
                input=block.get("input", {}),
            ))

    return AdapterResponse(
        content="\n".join(text_parts),
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        raw_content=content_blocks,
    )
