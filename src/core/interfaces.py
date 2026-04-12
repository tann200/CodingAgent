"""Core Protocol interfaces — TASK-2.

Defines structural (duck-typed) contracts for the three key seams in the
CodingAgent architecture:

    ApiClientProtocol   — any LLM adapter
    ToolExecutorProtocol — any tool-dispatch layer
    SessionStoreProtocol — any conversation-persistence backend

Using ``typing.Protocol`` with ``runtime_checkable`` means:
- No inheritance required — existing adapters satisfy the protocol
  automatically if their method signatures match.
- ``isinstance(obj, ApiClientProtocol)`` works at runtime (useful for tests
  and defensive guards).
- mypy / pyright can verify structural compatibility statically.

Equivalent to Rust's trait-based ``ConversationRuntime<C: ApiClient, T: ToolExecutor>``
in claw-code's architecture.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# ApiClientProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApiClientProtocol(Protocol):
    """Minimal contract that any LLM adapter must satisfy.

    All existing adapters (LmStudioAdapter, OllamaAdapter, AnthropicAdapter,
    OpenRouterAdapter, MockAdapter, …) satisfy this protocol without any
    code changes — they all implement ``generate()`` with compatible
    signatures.
    """

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        format_json: bool = False,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """Call the LLM and return a response object.

        Returns: an object with at least a ``content`` attribute (str or list).
        """
        ...

    @property
    def model_name(self) -> str:
        """Active model identifier (e.g. 'gemma-4-27b-it', 'gpt-4o')."""
        ...

    @property
    def provider_name(self) -> str:
        """Provider identifier (e.g. 'lm_studio', 'anthropic', 'openrouter')."""
        ...


# ---------------------------------------------------------------------------
# ToolExecutorProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Minimal contract for tool dispatch.

    Satisfied by ``ToolExecutionService`` and any mock/stub that exposes the
    same async ``execute`` method.
    """

    async def execute(
        self,
        name: str,
        args: Dict[str, Any],
        *,
        working_dir: Optional[str] = None,
    ) -> Any:
        """Execute a registered tool by name.

        Parameters
        ----------
        name:
            Registered tool name (e.g. ``"write_file"``).
        args:
            Tool arguments dict (must match the tool's declared schema).
        working_dir:
            Override the default working directory for this call.

        Returns
        -------
        Any
            Tool result — typically a dict with an ``"ok"`` bool and
            result fields, but the exact shape is tool-specific.
        """
        ...


# ---------------------------------------------------------------------------
# SessionStoreProtocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """Minimal contract for conversation persistence.

    Satisfied by both the existing SQLite ``SessionStore`` and the new
    ``JsonlSessionStore`` (TASK-7), enabling the orchestrator to accept
    either backend without code changes.
    """

    def add_message(self, session_id: str, role: str, content: str) -> None:
        """Append a conversation message to the store."""
        ...

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Return all messages for *session_id* in insertion order."""
        ...

    def fork_session(self, session_id: str, new_session_id: str) -> str:
        """Create an independent copy of *session_id* as *new_session_id*.

        Returns the new session ID.
        """
        ...

    def revert_session(self, session_id: str, snapshot_id: str) -> None:
        """Roll back *session_id* to the state captured in *snapshot_id*."""
        ...

    def save_snapshot(self, session_id: str, state_json: str) -> str:
        """Persist a snapshot of *state_json* and return the snapshot ID."""
        ...

    def get_snapshot(
        self, session_id: str, snapshot_id: str
    ) -> Optional[str]:
        """Return the state JSON for *snapshot_id*, or None if not found."""
        ...
