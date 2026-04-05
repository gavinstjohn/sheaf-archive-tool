from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterCapabilities:
    vision: bool = False
    tool_use: bool = True
    max_context: int = 200_000
    streaming: bool = False


@dataclass
class Message:
    role: str          # "user" or "assistant"
    content: str | list[dict]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class AdapterResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    # Raw content blocks (needed to faithfully reconstruct assistant message for tool loop)
    raw_content: list[dict] = field(default_factory=list)


class BaseAdapter(ABC):

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8096,
    ) -> AdapterResponse:
        """Send a conversation and return the response."""

    @property
    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Return what this model/adapter supports."""
