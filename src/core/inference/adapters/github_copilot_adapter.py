"""GitHub Copilot adapter.

Extends OpenAICompatibleAdapter — inherits HTTP retry logic (3 attempts with
exponential backoff), response normalisation, streaming, reasoning_content
fallback, and tool call extraction.

Only overrides:
  __init__            — config loading (base_url fixed, no static api_key)
  _headers()          — inject GitHub OAuth token + Copilot-specific headers
  _models_endpoints() — point to Copilot /models endpoint
  get_models_from_api()  — guard on is_authenticated(); delegate to base class
  validate_connection()  — return is_authenticated() (no network call at startup)

Auth flow (managed by github_copilot_auth module):
  GitHub OAuth device flow → access_token stored in UserPrefs
  → loaded per-request in _headers() as  Authorization: Bearer <token>
  The token is long-lived (no expiry unless user revokes in GitHub settings).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)

COPILOT_BASE_URL = "https://api.githubcopilot.com"
_EDITOR_VERSION  = "codingagent/1.0"
_PLUGIN_VERSION  = "codingagent-chat/1.0"
_INTEGRATION_ID  = "codingagent"
_USER_AGENT      = "CodingAgent/1.0"

# Static fallback model list used before /models is fetched or when unauthenticated.
_DEFAULT_MODELS = [
    "gpt-4o",
    "gpt-4o-mini",
    "claude-3.5-sonnet",
    "claude-3.7-sonnet",
    "o1",
    "o3-mini",
    "gemini-2.0-flash-001",
]


class GithubCopilotAdapter(OpenAICompatibleAdapter):
    """GitHub Copilot provider via OpenAI-compatible /chat/completions API."""

    REQUIRES_API_KEY = False      # replaced by OAuth device flow
    AUTH_FLOW = "github_device"   # TUI sentinel → renders Login button, not key field

    def __init__(
        self,
        models: Optional[List[str]] = None,
        name: str = "github_copilot",
        **kwargs,
    ):
        # Ignore base_url/api_key passed from providers.json — base URL is fixed
        # and the token is loaded dynamically per-request from UserPrefs.
        super().__init__(
            base_url=COPILOT_BASE_URL,
            api_key=None,
            default_model=None,
            models=list(models) if models else list(_DEFAULT_MODELS),
            name=name,
        )

    # ── Headers (called per-request by base class _safe_post) ─────────────────

    def _headers(self) -> Dict[str, str]:
        """Build request headers, loading the GitHub OAuth token from UserPrefs."""
        from src.core.inference.adapters.github_copilot_auth import load_token
        token = load_token()
        if not token:
            raise RuntimeError(
                "GitHub Copilot: not authenticated. "
                "Use 'Login with GitHub' in Settings to authorize."
            )
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
            "Editor-Version": _EDITOR_VERSION,
            "Editor-Plugin-Version": _PLUGIN_VERSION,
            "Copilot-Integration-Id": _INTEGRATION_ID,
            "Openai-Intent": "conversation-edits",
            "x-initiator": "user",
        }

    # ── Model discovery ────────────────────────────────────────────────────────

    def _models_endpoints(self) -> List[str]:
        return [f"{COPILOT_BASE_URL}/models"]

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


# Alias expected by ProviderManager module-level fallback lookup
Adapter = GithubCopilotAdapter
