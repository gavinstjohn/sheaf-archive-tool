from __future__ import annotations

import atexit
import itertools
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..adapter.base import AdapterResponse, BaseAdapter, Message, ToolCall, ToolDefinition

_HISTORY_FILE = Path.home() / ".sheaf_history"
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusLine:
    """Persistent single-line status indicator with spinner.

    Maintains a live spinning line on stderr that can be updated at any time.
    checkpoint() commits a permanent line (with newline) and keeps the spinner
    running below it, building up a visible activity log.

    Usage:
        with StatusLine() as status:
            status.update("thinking")
            ...do work...
            status.checkpoint("✓ some_tool (0.3s)")
            status.update("thinking")
            ...more work...
    """

    def __init__(self) -> None:
        self._message = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StatusLine":
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def update(self, message: str) -> None:
        """Update the spinning status line in place."""
        with self._lock:
            self._message = message

    def checkpoint(self, message: str) -> None:
        """Commit a permanent line to stderr and keep spinning below it."""
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write(f"\r\033[K  {message}\n")
        sys.stderr.flush()
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            with self._lock:
                msg = self._message
            sys.stderr.write(f"\r{frame} {msg}")
            sys.stderr.flush()
            time.sleep(0.08)


class _Spinner:
    """Simple spinner for standalone say() calls (no tool loop)."""

    def __init__(self, message: str = "thinking") -> None:
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Spinner":
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()

    def _spin(self) -> None:
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r{frame} {self._message}...")
            sys.stderr.flush()
            time.sleep(0.08)


def _setup_readline() -> None:
    """Enable readline line-editing and persistent cross-session history for input()."""
    try:
        import readline
        try:
            readline.read_history_file(_HISTORY_FILE)
        except FileNotFoundError:
            pass
        readline.set_history_length(1000)
        atexit.register(readline.write_history_file, _HISTORY_FILE)
    except ImportError:
        pass  # readline not available (e.g. Windows without pyreadline)

log = logging.getLogger(__name__)


_TOOL_RESULT_HISTORY_LIMIT = 300  # chars to retain in history after a result is processed


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

    def say(self, user_message: str, _status: StatusLine | None = None) -> AdapterResponse:
        """Add a user message and get one response (no tool loop)."""
        self._messages.append(Message(role="user", content=user_message))
        if _status:
            _status.update("thinking...")
            response = self._adapter.chat(
                self._messages,
                tools=self._tools or None,
                system=self._system,
                max_tokens=self._max_tokens,
            )
        else:
            with _Spinner():
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
        with StatusLine() as status:
            response = self.say(user_message, _status=status)

            while response.stop_reason == "tool_use" and response.tool_calls:
                tool_results = []
                for call in response.tool_calls:
                    status.update(f"→ {call.name}...")
                    t0 = time.monotonic()
                    result = self._dispatch(call)
                    elapsed = time.monotonic() - t0
                    if result.startswith("Error:"):
                        status.checkpoint(f"✗ {call.name} ({elapsed:.1f}s)  {result[:60]}")
                    else:
                        status.checkpoint(f"✓ {call.name} ({elapsed:.1f}s)")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": result,
                    })
                # Append all tool results as a single user message
                self._messages.append(Message(role="user", content=tool_results))

                status.update("thinking...")
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

                # After the model has processed the tool results, truncate their
                # content in history. The model already incorporated the data —
                # future turns don't need the full text, just a reminder it ran.
                self._trim_last_tool_results()

        return response.content

    def _trim_last_tool_results(self) -> None:
        """Truncate tool result content in the most recent tool-results message.

        Walks backward to find the last user message that contains tool results
        and truncates any long content strings to _TOOL_RESULT_HISTORY_LIMIT chars.
        """
        for msg in reversed(self._messages):
            if msg.role != "user" or not isinstance(msg.content, list):
                continue
            items = msg.content
            if not any(isinstance(i, dict) and i.get("type") == "tool_result" for i in items):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "tool_result":
                    continue
                content = item.get("content", "")
                if isinstance(content, str) and len(content) > _TOOL_RESULT_HISTORY_LIMIT:
                    kept = content[:_TOOL_RESULT_HISTORY_LIMIT]
                    dropped = len(content) - _TOOL_RESULT_HISTORY_LIMIT
                    item["content"] = kept + f"\n[…{dropped} chars — already processed]"
            break

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
    _setup_readline()

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
