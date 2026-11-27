"""
Utilities for handling LLM thinking/reasoning tokens.

Some models (Qwen3, DeepSeek-R1-Distill, OpenAI o-series) use a
"reasoning-first" pattern before their actual response.  These utilities let
the codebase:

  1. Detect whether the active model is a reasoning model.
  2. Strip <think> blocks from any response string.
  3. Choose an appropriate max_tokens budget (reasoning models need a larger
     budget so thinking tokens don't crowd out the real answer).
  4. Control thinking mode via CLI (--thinking auto|on|off)
"""

from __future__ import annotations

import re
import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ThinkingMode(str, Enum):
    OFF = "off"  # Never use thinking (small local models default)
    AUTO = "auto"  # Enabled for reasoning models or multi-step tasks
    ON = "on"  # Always on (debug, complex tasks)


# Substrings that identify models with automatic thinking-token generation or
# "reasoning-first" behaviour that requires the beast/reasoning prompt variant.
#
# Local reasoning models (Qwen3, DeepSeek-R1) emit <think> blocks.
# OpenAI o-series (o1, o3, o4) use server-side chain-of-thought; they do NOT
# emit <think> blocks but still need a different prompting style (no CoT
# instructions, direct output, no streaming tool-call format).
_REASONING_MODEL_PATTERNS = (
    "qwen3",  # Covers qwen3, qwen3.5, qwen3.5-9b, qwen3.8-27b
    "deepseek-r1",
    "deepseek_r1",
    "deepseek-reasoner",
    "qwq",
    # OpenAI o-series reasoning models
    "o1-",
    "o1-mini",
    "o1-preview",
    "o3-mini",
    "o3-",
    "o4-",
)

# Bare OpenAI o-series model names (e.g. "o1", "o3", "o4", "openai/o3", "o3:latest")
# matched on token boundaries so non-reasoning names like "gpt-4o" never match.
_BARE_O_SERIES_RE = re.compile(r"(?:^|[/:\s])o[134](?:$|[-\s:@])")

# Qwen3 specifically supports /no_think to suppress the think block entirely.
_NO_THINK_SUPPORTED_PATTERNS = (
    "qwen3",  # Covers qwen3, qwen3.5
    "qwq",
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove all <think>...</think> blocks from *text*, including nested blocks.

    Safe to call on any model's output — non-thinking models produce no such
    blocks so the string is returned unchanged.

    C-02: naive non-greedy regex leaves dangling ``</think>`` when tags are
    nested (e.g. ``<think>outer <think>inner</think> still outer</think>``).
    We loop until the string no longer changes so every nesting level is
    stripped, then remove any leftover ``</think>`` closing tags.
    """
    prev = None
    while prev != text:
        prev = text
        text = _THINK_RE.sub("", text)
    # Remove any orphaned closing tags left after stripping nested blocks
    text = re.sub(r"</think>", "", text, flags=re.IGNORECASE)
    return text.strip()


def is_reasoning_model(model_id: str) -> bool:
    """Return True when *model_id* is a known reasoning-first model."""
    # BUG-FIX: handle non-string input gracefully
    lowered = str(model_id).lower().strip() if model_id is not None else ""
    if not lowered:
        return False
    return any(pat in lowered for pat in _REASONING_MODEL_PATTERNS) or bool(
        _BARE_O_SERIES_RE.search(lowered)
    )


def supports_no_think(model_id: str) -> bool:
    """Return True when *model_id* honours the /no_think prompt directive."""
    # BUG-FIX: handle non-string input gracefully
    lowered = str(model_id).lower() if model_id is not None else ""
    return any(pat in lowered for pat in _NO_THINK_SUPPORTED_PATTERNS)


def budget_max_tokens(base: int, model_id: str) -> int:
    """Return an adjusted max_tokens budget for *model_id*.

    For reasoning models that cannot suppress thinking tokens (e.g.
    DeepSeek-R1-Distill), the thinking block may consume most of the token
    budget before the real answer starts.  We double the allocation so the
    final JSON/text output is not truncated.

    For models where /no_think works (Qwen3), the base budget is sufficient.
    For all other models there is no overhead, so the base is returned as-is.
    """
    if is_reasoning_model(model_id) and not supports_no_think(model_id):
        adjusted = base * 2
        logger.debug(
            f"thinking_utils: doubling max_tokens {base} → {adjusted} for reasoning model '{model_id}'"
        )
        return adjusted
    return base


def get_active_model_id() -> str:
    """Best-effort lookup of the currently configured model ID.

    Returns an empty string if the model cannot be determined (e.g. during
    testing or before the provider is initialised).
    """
    try:
        from src.core.inference.llm_manager import load_provider

        raw = load_provider(None)
        providers = (
            raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        )
        if providers:
            p = providers[0]
            model_id = p.get("model") or p.get("default_model") or p.get("name") or ""
            return str(model_id)
    except Exception:
        pass
    return ""


_ACTIVE_THINKING_MODE: ThinkingMode | None = None


def set_active_thinking_mode(mode: ThinkingMode | str | None) -> None:
    """Set the globally active thinking mode from CLI or configuration."""
    global _ACTIVE_THINKING_MODE
    if mode is None:
        _ACTIVE_THINKING_MODE = None
    elif isinstance(mode, ThinkingMode):
        _ACTIVE_THINKING_MODE = mode
    else:
        try:
            _ACTIVE_THINKING_MODE = ThinkingMode(str(mode).lower())
        except ValueError:
            logger.warning(f"Invalid thinking mode: {mode}")
            _ACTIVE_THINKING_MODE = None


def get_active_thinking_mode() -> ThinkingMode | None:
    """Get the currently active thinking mode, if any."""
    return _ACTIVE_THINKING_MODE


def resolve_thinking_mode(cli_mode: ThinkingMode | str | None = None, model_id: str = "") -> bool:
    """Resolve whether thinking should be enabled for this model.

    Args:
        cli_mode: CLI --thinking flag value (None = use active thinking mode or default)
        model_id: Active model identifier

    Returns:
        True if thinking should be enabled, False otherwise
    """
    effective_mode: ThinkingMode | None
    if cli_mode is None:
        effective_mode = _ACTIVE_THINKING_MODE
    elif isinstance(cli_mode, ThinkingMode):
        effective_mode = cli_mode
    else:
        try:
            effective_mode = ThinkingMode(str(cli_mode).lower())
        except ValueError:
            effective_mode = _ACTIVE_THINKING_MODE

    if effective_mode == ThinkingMode.ON:
        return True
    if effective_mode == ThinkingMode.OFF:
        return False

    # AUTO mode: enable for reasoning models
    if effective_mode == ThinkingMode.AUTO or effective_mode is None:
        return is_reasoning_model(model_id)

    return False


def get_thinking_directive(model_id: str, enabled: bool) -> str | None:
    """Get the prompt directive to control thinking mode.

    Args:
        model_id: Active model identifier
        enabled: Whether thinking should be enabled

    Returns:
        Prompt directive string, or None if model doesn't support it
    """
    if not enabled and supports_no_think(model_id):
        return "/no_think"
    if enabled and model_id.lower().startswith("qwen3"):
        return None  # Qwen3 thinks by default, no directive needed
    return None


# ---------------------------------------------------------------------------
# TASK-29: Unified Reasoning Effort & Dynamic Thinking Budget Control
# ---------------------------------------------------------------------------


class ReasoningEffort(str, Enum):
    """Portable reasoning/thinking effort level across local & cloud providers."""

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNSET = "unset"


_REASONING_BUDGET_TOKENS: dict[ReasoningEffort, int] = {
    ReasoningEffort.OFF: 0,
    ReasoningEffort.LOW: 1024,
    ReasoningEffort.MEDIUM: 2560,
    ReasoningEffort.HIGH: 6144,
}

_ACTIVE_REASONING_EFFORT: ReasoningEffort | None = None


def set_active_reasoning_effort(effort: ReasoningEffort | str | None) -> ReasoningEffort:
    """Set the globally active reasoning effort (CLI ``--reasoning`` or ``/reasoning``)."""
    global _ACTIVE_REASONING_EFFORT
    if effort is None:
        _ACTIVE_REASONING_EFFORT = None
        return ReasoningEffort.UNSET
    if isinstance(effort, ReasoningEffort):
        _ACTIVE_REASONING_EFFORT = None if effort == ReasoningEffort.UNSET else effort
        return effort
    norm = str(effort).strip().lower()
    if norm in ("reset", "unset", "default", ""):
        _ACTIVE_REASONING_EFFORT = None
        return ReasoningEffort.UNSET
    if norm == "none":
        norm = "off"
    resolved = ReasoningEffort(norm)
    _ACTIVE_REASONING_EFFORT = resolved
    return resolved


def get_active_reasoning_effort() -> ReasoningEffort | None:
    """Return the active global reasoning effort override, or ``None`` if unset."""
    return _ACTIVE_REASONING_EFFORT


def reasoning_budget_tokens(effort: ReasoningEffort) -> int | None:
    """Return the target thinking-token budget for *effort* (``None`` for UNSET)."""
    return _REASONING_BUDGET_TOKENS.get(effort)


def resolve_reasoning_effort(
    *,
    cli_effort: ReasoningEffort | str | None = None,
    session_effort: ReasoningEffort | str | None = None,
    model_id: str = "",
    tier: str | None = None,
    verification_retries: int = 0,
    disable_thinking: bool = False,
) -> ReasoningEffort:
    """Resolve effective reasoning effort with precedence and retry bumping.

    Precedence:
      1. Explicit ``disable_thinking=True`` → ``OFF``
      2. ``cli_effort``
      3. ``session_effort``
      4. Active global override (``/reasoning`` / ``--reasoning``)
      5. Tier default: ``compact``/``nano`` reasoning models default to ``LOW``
         so distilled 9B models don't exhaust context/latency on routine edits;
         other tiers default to ``UNSET``.

    When ``verification_retries >= 1``, a ``LOW`` cap automatically bumps to
    ``MEDIUM`` (and ``MEDIUM`` → ``HIGH``) to give deeper reasoning on fix turns.
    """
    if disable_thinking:
        return ReasoningEffort.OFF

    def _coerce(val: ReasoningEffort | str | None) -> ReasoningEffort | None:
        if val is None:
            return None
        if isinstance(val, ReasoningEffort):
            return None if val == ReasoningEffort.UNSET else val
        s = str(val).strip().lower()
        if s in ("", "unset", "reset", "default"):
            return None
        if s == "none":
            return ReasoningEffort.OFF
        try:
            return ReasoningEffort(s)
        except ValueError:
            return None

    chosen = (
        _coerce(cli_effort)
        or _coerce(session_effort)
        or _ACTIVE_REASONING_EFFORT
    )

    if chosen is None:
        # Respect legacy ThinkingMode.OFF if explicitly set
        if _ACTIVE_THINKING_MODE == ThinkingMode.OFF:
            return ReasoningEffort.OFF
        norm_tier = (tier or "").strip().lower()
        if norm_tier in ("nano", "compact", "compact-flat") and is_reasoning_model(model_id):
            chosen = ReasoningEffort.LOW
        else:
            return ReasoningEffort.UNSET

    if verification_retries >= 1:
        if chosen == ReasoningEffort.LOW:
            chosen = ReasoningEffort.MEDIUM
        elif chosen == ReasoningEffort.MEDIUM:
            chosen = ReasoningEffort.HIGH

    return chosen


def apply_reasoning_to_payload(
    provider_kind: str,
    payload: dict,
    effort: ReasoningEffort,
    *,
    model_id: str = "",
) -> dict:
    """Apply portable *effort* defensively to a provider-specific request payload."""
    if effort == ReasoningEffort.UNSET:
        return payload

    kind = (provider_kind or "").strip().lower().replace("-", "_")
    budget = reasoning_budget_tokens(effort)

    if kind in ("llama_server", "llamacpp", "llama_cpp"):
        ctk = payload.get("chat_template_kwargs")
        if not isinstance(ctk, dict):
            ctk = {}
            payload["chat_template_kwargs"] = ctk
        if effort == ReasoningEffort.OFF:
            ctk.setdefault("enable_thinking", False)
            payload.setdefault("think", False)
            payload.setdefault("reasoning_budget", 0)
        else:
            ctk.setdefault("enable_thinking", True)
            if budget is not None:
                payload.setdefault("reasoning_budget", budget)
                cap = budget + 1024
                cur_max = payload.get("max_tokens")
                if cur_max is None or (isinstance(cur_max, int) and cur_max > cap):
                    payload["max_tokens"] = cap
        return payload

    if kind == "ollama":
        if effort == ReasoningEffort.OFF:
            payload.setdefault("think", False)
        else:
            payload.setdefault("think", True)
            if budget is not None:
                opts = payload.get("options")
                if not isinstance(opts, dict):
                    opts = {}
                    payload["options"] = opts
                cap = budget + 1024
                cur_pred = opts.get("num_predict")
                if cur_pred is None or (isinstance(cur_pred, int) and cur_pred > cap):
                    opts["num_predict"] = cap
        return payload

    if kind in ("anthropic",):
        if effort != ReasoningEffort.OFF and budget is not None and budget > 0:
            payload.setdefault("thinking", {"type": "enabled", "budget_tokens": budget})
        return payload

    if kind in ("openai", "openrouter"):
        if effort == ReasoningEffort.OFF:
            if is_reasoning_model(model_id):
                payload.setdefault("reasoning_effort", "none")
        else:
            if kind == "openrouter" or is_reasoning_model(model_id):
                payload.setdefault("reasoning_effort", effort.value)
        return payload

    return payload
