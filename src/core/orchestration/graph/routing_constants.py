"""Single source of truth for routing thresholds (audit item 2.7).

All routing modules import their caps/limits here instead of inlining magic
numbers, so each threshold is defined exactly once.  Keep this module free of
runtime side effects — it only declares constants used by the routers.
"""

# ── tool-call budget ──
DEFAULT_MAX_TOOL_CALLS = 30  # default tool-call budget for the standard graph
LOOP_GUARD_ROUNDS = 10  # round threshold for stuck-loop detection

# ── per-tier recovery caps ──
RECOVERY_CAPS: dict[str, int] = {
    "small": 4,
    "medium": 8,
    "large": 12,
    "frontier": 12,
}
DEFAULT_RECOVERY_CAP = 8  # fallback recovery cap for unknown tiers

# ── replan budget ──
REPLAN_CAP_LARGE_FRONTIER = 3  # replan attempt cap for large/frontier tiers
REPLAN_CAP_OTHER = 5  # replan attempt cap for other tiers

# ── per-step retry budget ──
MAX_STEP_RETRIES = 3  # max retries per execution/plan step before bailing

# ── no-plan failure tolerance ──
MAX_NO_PLAN_FAILS = 3  # no-plan execution failures before bailing to memory_sync

# ── debug budget ──
DEFAULT_MAX_DEBUG_ATTEMPTS = 3  # default max debug attempts when unset in state
TOTAL_DEBUG_CAP_LARGE_FRONTIER = 5  # total-debug cap for large/frontier tiers
TOTAL_DEBUG_CAP_OTHER = 9  # total-debug cap for other tiers

# ── planning budget ──
MAX_ROUNDS_PLANNING = 15  # force-end after this many planning rounds
MAX_PLAN_ATTEMPTS = 3  # force execution after this many plan attempts
FORCE_EXECUTION_ROUNDS = 8  # force execution past this many rounds
