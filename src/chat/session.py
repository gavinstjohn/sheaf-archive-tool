from __future__ import annotations

import json
import logging
from typing import Callable

from ..adapter.base import AdapterResponse, BaseAdapter, Message, ToolCall, ToolDefinition

log = logging.getLogger(__name__)


class ChatSession:
    """Stateful conversation session backed by an adapter.

    Manages message history and tool dispatch. Call say() for a single
    round-trip, or tool_loop() to handle tool calls automatically until
    the model returns a plain text response.
    """

    def __init__(
        self,
        adapter: BaseAdapter,
        system: str | None = None,
        max_tokens: int = 8096,
    ) -> None:
        self._adapter = adapter
        self._system = system
        self._max_tokens = max_tokens
        self._messages: list[Message] = []
        self._tools: list[ToolDefinition] = []
        self._handlers: dict[str, Callable[[dict], str]] = {}
        # Set to True by a tool handler to signal the conversation is done
        self.done: bool = False

    # ------------------------------------------------------------------
    # Tool registration
    # ------------------------------------------------------------------

    def register_tool(self, defn: ToolDefinition, handler: Callable[[dict], str]) -> None:
        self._tools.append(defn)
        self._handlers[defn.name] = handler

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    def say(self, user_message: str) -> AdapterResponse:
        """Add a user message and get one response (no tool loop)."""
        self._messages.append(Message(role="user", content=user_message))
        response = self._adapter.chat(
            self._messages,
            tools=self._tools or None,
            system=self._system,
            max_tokens=self._max_tokens,
        )
        # Store assistant message with full content blocks so tool results
        # can be appended correctly in subsequent turns.
        self._messages.append(Message(
            role="assistant",
            content=response.raw_content if response.raw_content else response.content,
        ))
        return response

    def tool_loop(self, user_message: str) -> str:
        """Send a user message and run the tool dispatch loop until the model
        returns a plain text response (stop_reason != 'tool_use').

        Returns the final text content.
        """
        response = self.say(user_message)

        while response.stop_reason == "tool_use" and response.tool_calls:
            tool_results = []
            for call in response.tool_calls:
                result = self._dispatch(call)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": result,
                })
            # Append all tool results as a single user message
            self._messages.append(Message(role="user", content=tool_results))

            response = self._adapter.chat(
                self._messages,
                tools=self._tools or None,
                system=self._system,
                max_tokens=self._max_tokens,
            )
            self._messages.append(Message(
                role="assistant",
                content=response.raw_content if response.raw_content else response.content,
            ))

        return response.content

    def inject_assistant(self, text: str) -> None:
        """Inject an assistant message without calling the API (for seeding context)."""
        self._messages.append(Message(role="assistant", content=text))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _dispatch(self, call: ToolCall) -> str:
        handler = self._handlers.get(call.name)
        if handler is None:
            return f"Error: unknown tool {call.name!r}"
        try:
            log.debug("Tool call: %s(%s)", call.name, call.input)
            result = handler(call.input)
            log.debug("Tool result: %s → %s", call.name, result[:200] if result else "")
            return result
        except Exception as e:
            log.warning("Tool %s raised: %s", call.name, e)
            return f"Error: {e}"


def readline_chat(session: ChatSession, initial_message: str | None = None) -> None:
    """Simple REPL loop. Prints assistant responses and reads user input.

    The loop ends when the user types 'done' or 'quit', or when
    session.done is set to True by a tool handler.
    """
    if initial_message:
        print(f"\nSheaf: {initial_message}\n")

    while not session.done:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in ("done", "quit", "exit"):
            break

        response = session.tool_loop(user_input)
        if response:
            print(f"\nSheaf: {response}\n")

        if session.done:
            break
