"""
src/memory.py
─────────────────────────────────────────────────────────────
Conversation memory management for the support agent.

Keeps a bounded history of messages for multi-turn conversations.
History lives only in session state — nothing is persisted to disk.
─────────────────────────────────────────────────────────────
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.logger import get_logger, log_event

logger = get_logger(__name__)


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass(frozen=True)
class Message:
    """A single message in the conversation."""
    role: Role
    content: str


@dataclass
class ConversationMemory:
    """
    Manages bounded conversation history for a single chat session.

    The history is kept to a maximum length to avoid exceeding
    LLM context limits and to keep token costs predictable.
    Older messages are dropped from the front when the limit is reached.
    """

    max_history: int = 10
    _messages: list[Message] = field(default_factory=list, repr=False)

    def add_user_message(self, content: str) -> None:
        """Add a user message to the history."""
        self._append(Message(role=Role.USER, content=content))

    def add_assistant_message(self, content: str) -> None:
        """Add an assistant response to the history."""
        self._append(Message(role=Role.ASSISTANT, content=content))

    def _append(self, message: Message) -> None:
        """
        Append a message and trim history if it exceeds max_history.
        Trims from the front, preserving the most recent exchanges.
        """
        self._messages.append(message)
        if len(self._messages) > self.max_history:
            removed = len(self._messages) - self.max_history
            self._messages = self._messages[removed:]
            log_event(
                logger,
                "memory_trimmed",
                removed=removed,
                current_length=len(self._messages),
            )

    def get_history(self) -> list[Message]:
        """Return the current conversation history as a list of Messages."""
        return list(self._messages)

    def get_history_as_dicts(self) -> list[dict]:
        """
        Return history formatted for the OpenAI messages API.

        Returns:
            List of {"role": str, "content": str} dicts.
        """
        return [
            {"role": msg.role.value, "content": msg.content}
            for msg in self._messages
        ]

    def clear(self) -> None:
        """Clear all conversation history."""
        count = len(self._messages)
        self._messages.clear()
        log_event(logger, "memory_cleared", messages_removed=count)

    @property
    def message_count(self) -> int:
        """Number of messages currently in history."""
        return len(self._messages)

    @property
    def is_empty(self) -> bool:
        """True if there are no messages in history."""
        return len(self._messages) == 0
