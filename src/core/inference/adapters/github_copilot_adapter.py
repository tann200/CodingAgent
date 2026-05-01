"""GitHub Copilot adapter.

Extends OpenAICompatibleAdapter — inherits HTTP retry logic (3 attempts with
exponential backoff), response normalisation, streaming, reasoning_content
fallback, and tool call extraction.

Only overrides:
  __init__            — config loading (base_url fixed, no static api_key)
  _compose()          — direct path append (no /api/v1/ injection)
  _headers()          — inject GitHub OAuth token + Copilot-specific headers
                         (matches OpenCode's copilot.ts loader() exactly)
  _chat_internal()    — remove deprecated functions key, handle 401 re-auth
  _models_endpoints() — point to Copilot /models endpoint
  get_models_from_api()  — guard on is_authenticated(); delegate to base class
  validate_connection()  — return is_authenticated() (no network call at startup)

Auth flow (managed by github_copilot_auth module):
  GitHub OAuth device flow → access_token stored in
  ~/.local/share/codingagent/auth.json under key "github-copilot"
  → loaded per-request in _headers() as  Authorization: Bearer <token>

Headers sent on every request (mirrors OpenCode copilot.ts):
  Authorization: Bearer <github_oauth_token>
  User-Agent: CodingAgent/1.0
  Openai-Intent: conversation-edits
  x-initiator: user | agent   (based on last message role)
  (x-api-key and lowercase authorization are NOT sent — Copilot rejects them)

GitHub Enterprise: if the stored auth.json entry has an enterpriseUrl field,
the base URL is overridden to  https://copilot-api.<domain>.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Union

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)

# Thread-local store for per-request context stashed by _chat_internal()
# so _headers() can read it without instance-level data races.
_tl = threading.local()

COPILOT_BASE_URL = "https://api.githubcopilot.com"
_USER_AGENT = "CodingAgent/1.0"

# Static fallback model list used before /models is fetched or when unauthenticated.
_DEFAULT_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3.5-sonnet",
    "claude-3.7-sonnet",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    "o1",
    "o3-mini",
    "gemini-2.0-flash-001",
]


class GithubCopilotAdapter(OpenAICompatibleAdapter):
    """GitHub Copilot provider via OpenAI-compatible /chat/completions API."""

    REQUIRES_API_KEY = False  # replaced by OAuth device flow
    AUTH_FLOW = "github_device"  # TUI sentinel → renders Login button, not key field

    def __init__(
        self,
        models: Optional[List[str]] = None,
        name: str = "github_copilot",
        **kwargs,
    ):
        # Determine base URL — may be overridden for GitHub Enterprise
        from src.core.inference.adapters.github_copilot_auth import load_enterprise_url

        enterprise = load_enterprise_url()
        base_url = (
            f"https://copilot-api.{enterprise}" if enterprise else COPILOT_BASE_URL
        )

        # Ignore base_url/api_key passed from providers.json — base URL is fixed
        # and the token is loaded dynamically per-request from auth.json.
        super().__init__(
            base_url=base_url,
            api_key=None,
            default_model=None,
            models=list(models) if models else list(_DEFAULT_MODELS),
            name=name,
        )

    # ── URL composition ───────────────────────────────────────────────────────

    def _compose(self, path: str) -> Optional[str]:
        """Append path directly to base_url without injecting /api/v1/.

        The base OpenAICompatibleAdapter._compose() adds /api/v1/ for URLs
        that don't contain a version segment, which breaks the Copilot API
        (both public https://api.githubcopilot.com and enterprise
        https://copilot-api.<domain>) — CP-01.
        """
        if not self.base_url:
            return None
        return f"{str(self.base_url).rstrip('/')}/{path.lstrip('/')}"

    # ── Headers (called per-request by base class _safe_post) ─────────────────

    def _headers(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
    ) -> Dict[str, str]:
        """Build request headers, loading the GitHub OAuth token from auth.json.

        Header set matches OpenCode's copilot.ts loader() fetch() function:
          - Authorization: Bearer <github_oauth_token>
          - User-Agent: CodingAgent/1.0
          - Openai-Intent: conversation-edits
          - x-initiator: user | agent  (CP-02: dynamic based on last role)
          - Copilot-Vision-Request: true  (CP-03: when image content present)
          - anthropic-beta: …            (CP-04: for Claude models)
          x-api-key and lowercase 'authorization' are intentionally absent.

        The base class calls ``_headers()`` with no arguments during the retry
        loop.  We work around this by stashing the current request's messages
        and model on ``self`` in ``_chat_internal()`` before delegating to the
        base class, then reading them back here.
        """
        from src.core.inference.adapters.github_copilot_auth import load_token

        token = load_token()
        if not token:
            raise RuntimeError(
                "GitHub Copilot: not authenticated. "
                "Use 'Login with GitHub' in Settings to authorize."
            )

        # Fall back to thread-local context set by _chat_internal()
        msgs = (
            messages
            if messages is not None
            else getattr(_tl, "pending_messages", None)
        )
        mdl = model if model is not None else getattr(_tl, "pending_model", None)

        # CP-02: x-initiator reflects whether the last turn was from the user
        # or from the model (agent turn).  Matches OpenCode copilot.ts logic.
        initiator = "user"
        if msgs:
            last_role = msgs[-1].get("role", "user") if msgs else "user"
            if last_role == "assistant":
                initiator = "agent"

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
            "Openai-Intent": "conversation-edits",
            "x-initiator": initiator,
        }

        # CP-03: signal vision content so Copilot routes to a vision-capable model
        if msgs and _has_image_content(msgs):
            headers["Copilot-Vision-Request"] = "true"

        # CP-04: Claude models require the anthropic-beta preview header
        if mdl and "claude" in mdl.lower():
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"

        return headers

    # ── Inference ─────────────────────────────────────────────────────────────

    def _chat_internal(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model: Optional[str] = None,
        stream: bool = False,
        format_json: bool = False,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        """Override to:
        - Stash messages/model so _headers() can access them (CP-02/03/04)
        - Handle 401 Unauthorized by clearing the stale token (CP-07)
        """
        # Stash for _headers() which is called with no args by the base retry loop.
        # Use thread-local storage to avoid data races when the adapter is shared.
        _tl.pending_messages = messages if isinstance(messages, list) else None
        resolved_model = model or (
            self.models[0] if isinstance(self.models, list) and self.models else None
        )
        if resolved_model:
            resolved_model = self.resolve_model_name(resolved_model)
        _tl.pending_model = resolved_model

        result = super()._chat_internal(
            messages=messages,
            model=model,
            stream=stream,
            format_json=format_json,
            timeout=timeout,
            **kwargs,
        )

        # CP-07: if the base class returned a 401 meta error, the stored token is
        # stale (revoked or expired).  Clear it so the next request will trigger
        # the re-auth flow rather than silently failing.
        if isinstance(result, dict):
            meta = result.get("meta", {})
            if isinstance(meta, dict) and meta.get("status_code") == 401:
                _logger.warning(
                    "github_copilot_adapter: 401 Unauthorized — clearing stale token"
                )
                try:
                    from src.core.inference.adapters.github_copilot_auth import (
                        clear_token,
                    )

                    clear_token()
                except Exception:
                    pass
                result["user_message"] = (
                    "GitHub Copilot: your token has expired or been revoked. "
                    "Please log in again via Settings → GitHub Copilot → Login."
                )

        return result

    # ── _safe_post hook ────────────────────────────────────────────────────────

    def _safe_post(self, url, headers, payload, timeout=None, stream=False):  # type: ignore[override]
        """Strip deprecated ``functions`` key from payload before sending (CP-06).

        The base class adds ``functions`` as a copy of ``tools`` for OpenAI
        legacy compatibility.  The Copilot API rejects payloads that contain
        both ``tools`` and ``functions``.
        """
        if isinstance(payload, dict):
            payload.pop("functions", None)
        return super()._safe_post(url, headers, payload, timeout=timeout, stream=stream)

    # ── Model discovery ────────────────────────────────────────────────────────

    def _models_endpoints(self) -> List[str]:
        return [f"{self.base_url}/models"]

    def get_models_from_api(self) -> Dict[str, Any]:
        """GET /models — returns {"models": [{name, id, display_name, key}, ...]}.

        Returns empty list (not an error) when unauthenticated so startup health
        checks don't produce spurious warnings.
        """
        from src.core.inference.adapters.github_copilot_auth import is_authenticated

        if not is_authenticated():
            return {"models": []}
        # Base class handles {"data": [...]} and {"models": [...]} response shapes.
        result = super().get_models_from_api()
        if not result.get("models"):
            # Fall back to static list so the model dropdown is never empty
            result = {
                "models": [
                    {"name": m, "display_name": m, "id": m, "key": m}
                    for m in _DEFAULT_MODELS
                ]
            }
        return result

    # ── Connection check ───────────────────────────────────────────────────────

    def validate_connection(self) -> bool:
        """Return True if a token is stored (no network call at startup)."""
        from src.core.inference.adapters.github_copilot_auth import is_authenticated

        return is_authenticated()


# ── Helpers ────────────────────────────────────────────────────────────────────


def _has_image_content(messages: List[Dict[str, Any]]) -> bool:
    """Return True if any message contains an image_url content part."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


# Alias expected by ProviderManager module-level fallback lookup
Adapter = GithubCopilotAdapter
