"""
permission_policy.py — Config-level wildcard permission rules (Sprint A-4).

Implements PERM-01 through PERM-05 from docs/orchestration-gap-analysis.md.

Design
------
A PermissionPolicy is an ordered list of rules, each specifying:
  - a glob-style pattern that matches one or more tool names
  - a behavior: "allow", "deny", or "ask"

Evaluation uses **last-matching-wins** semantics, which means more-specific
rules placed later in the list take precedence over earlier broad rules:

    [
        {"pattern": "*",          "behavior": "ask"},   # default: ask for everything
        {"pattern": "read_*",     "behavior": "allow"}, # read tools are fine
        {"pattern": "bash",       "behavior": "ask"},   # but bash always prompts
        {"pattern": "doom_loop",  "behavior": "deny"},  # special doom-loop guard
    ]

The special pattern ``"doom_loop"`` is reserved: when matched it signals that
the doom-loop detector has flagged a looping tool sequence.  Sprint B will call
``policy.check("doom_loop")`` when loop detection fires.

Integration points
------------------
- Sits *above* ``ToolPermissionContext`` (CLI flags).  When both are present
  the union of restrictions is applied: a tool is blocked if either layer
  says "deny".
- Sits *above* ``AgentDefinition.is_tool_permitted()`` — agent-level rules are
  checked first (they are the tightest constraint); PermissionPolicy is checked
  next.
- The "ask" behavior is the hook for a future interactive confirmation dialog.
  For now, ``Behavior.ASK`` is treated as ``Behavior.ALLOW`` at runtime; the
  caller can inspect the behavior and prompt the user if desired.

Usage
-----
    from src.core.orchestration.permission_policy import (
        PermissionPolicy, PermissionRule, Behavior, get_permission_policy
    )

    policy = get_permission_policy()          # singleton, loads get_permissions_path() (user-level permissions.json)
    decision = policy.check("write_file")     # Behavior.ALLOW / .DENY / .ASK
    if decision == Behavior.DENY:
        raise ToolDeniedError(...)
"""

from __future__ import annotations

import fnmatch
import json
import logging
import traceback
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from src.core.paths import get_permissions_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Behavior enum
# ---------------------------------------------------------------------------


class Behavior(str, Enum):
    """Possible outcomes of a permission rule check.

    Using ``str`` as the base makes Behavior values JSON-serialisable
    and comparable to plain strings (e.g. ``Behavior.ALLOW == "allow"``).
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

    @classmethod
    def _missing_(cls, value: object) -> Optional["Behavior"]:
        """Accept case-insensitive string values."""
        if isinstance(value, str):
            for member in cls:
                if member.value == value.lower():
                    return member
        return None


# ---------------------------------------------------------------------------
# PermissionRule
# ---------------------------------------------------------------------------

# The reserved token used by Sprint B doom-loop detection.
DOOM_LOOP_TOKEN = "doom_loop"


@dataclass(frozen=True)
class PermissionRule:
    """A single permission rule: a glob pattern and a behavior.

    Attributes
    ----------
    pattern:
        Glob-style pattern (fnmatch).  Examples: ``"*"``, ``"write_*"``,
        ``"bash"``, ``"doom_loop"``.  Matching is case-insensitive.
    behavior:
        The action to take when the pattern matches: ALLOW, DENY, or ASK.
    """

    pattern: str
    behavior: Behavior

    def matches(self, tool_name: str) -> bool:
        """Return True if *tool_name* matches this rule's pattern."""
        return fnmatch.fnmatch(tool_name.lower(), self.pattern.lower())

    def to_dict(self) -> Dict[str, str]:
        return {"pattern": self.pattern, "behavior": self.behavior.value}

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "PermissionRule":
        pattern = str(data.get("pattern", "*"))
        raw_behavior = data.get("behavior", "allow")
        try:
            behavior = Behavior(str(raw_behavior).lower())
        except ValueError:
            logger.warning(
                "PermissionRule: unknown behavior %r; defaulting to 'allow'",
                raw_behavior,
            )
            behavior = Behavior.ALLOW
        return cls(pattern=pattern, behavior=behavior)


# ---------------------------------------------------------------------------
# PermissionPolicy
# ---------------------------------------------------------------------------


class PermissionPolicy:
    """An ordered list of PermissionRule objects.

    Rules are evaluated in order; **last matching rule wins**.  If no rule
    matches, the default behavior is ALLOW (open by default).

    Thread safety
    -------------
    Instances are effectively immutable after construction — ``_rules`` is
    never mutated after ``__init__``.  Concurrent reads are safe.
    """

    def __init__(
        self,
        rules: Optional[Sequence[PermissionRule]] = None,
        *,
        default_behavior: Behavior = Behavior.ALLOW,
    ) -> None:
        self._rules: Tuple[PermissionRule, ...] = tuple(rules or [])
        self._default = default_behavior

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def check(self, tool_name: str) -> Behavior:
        """Return the effective Behavior for *tool_name*.

        Iterates all rules and keeps the *last* matching one (later rules
        override earlier rules).  Falls back to ``default_behavior`` when
        nothing matches.
        """
        result = self._default
        for rule in self._rules:
            if rule.matches(tool_name):
                result = rule.behavior
        return result

    def is_denied(self, tool_name: str) -> bool:
        """Convenience: return True if *tool_name* is hard-denied."""
        return self.check(tool_name) == Behavior.DENY

    def is_allowed(self, tool_name: str) -> bool:
        """Convenience: return True if *tool_name* is allowed (ALLOW or ASK)."""
        return not self.is_denied(tool_name)

    def requires_confirmation(self, tool_name: str) -> bool:
        """Return True if the tool should prompt the user before executing."""
        return self.check(tool_name) == Behavior.ASK

    # ------------------------------------------------------------------
    # Doom-loop integration (PERM-05)
    # ------------------------------------------------------------------

    def check_doom_loop(self) -> Behavior:
        """Check the special 'doom_loop' token.

        Called by the doom-loop detector (Sprint B) when a looping tool
        sequence is detected.  Returns DENY by default (built-in protection)
        unless the caller has explicitly overridden the doom_loop rule.
        """
        # The built-in rule is DENY unless the user has overridden it.
        behavior = self.check(DOOM_LOOP_TOKEN)
        # If no explicit rule matched doom_loop (i.e. we got the default),
        # return DENY as the safe default for loop detection.
        if behavior == self._default:
            # Check whether any rule explicitly targets doom_loop.
            explicit = [r for r in self._rules if r.matches(DOOM_LOOP_TOKEN)]
            if not explicit:
                return Behavior.DENY
        return behavior

    # ------------------------------------------------------------------
    # Merging with ToolPermissionContext
    # ------------------------------------------------------------------

    def combined_check(
        self, tool_name: str, cli_context: Optional[object] = None
    ) -> Behavior:
        """Check *tool_name* against both this policy and a CLI context.

        If either source says DENY, the result is DENY.
        If either says ASK (and neither says DENY), the result is ASK.
        Otherwise ALLOW.

        Parameters
        ----------
        cli_context:
            A ``ToolPermissionContext`` instance from ``src.tools.permission_context``.
            Typed as ``object`` to avoid a circular import.
        """
        policy_behavior = self.check(tool_name)

        if policy_behavior == Behavior.DENY:
            return Behavior.DENY

        if cli_context is not None:
            try:
                if cli_context.blocks(tool_name):  # type: ignore[union-attr]
                    return Behavior.DENY
            except Exception:
                pass

        return policy_behavior

    # ------------------------------------------------------------------
    # Iteration / introspection
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[PermissionRule]:
        return iter(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def __repr__(self) -> str:
        return f"PermissionPolicy(rules={len(self._rules)}, default={self._default.value!r})"

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, object]:
        return {
            "default_behavior": self._default.value,
            "rules": [r.to_dict() for r in self._rules],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "PermissionPolicy":
        raw_default = data.get("default_behavior", "allow")
        try:
            default = Behavior(str(raw_default).lower())
        except ValueError:
            logger.warning(
                "PermissionPolicy.from_dict: unknown default_behavior %r; using 'allow'",
                raw_default,
            )
            default = Behavior.ALLOW
        raw_rules = data.get("rules", [])
        if not isinstance(raw_rules, list):
            logger.warning(
                "PermissionPolicy.from_dict: 'rules' must be a list; ignoring"
            )
            raw_rules = []
        rules: List[PermissionRule] = []
        for item in raw_rules:
            try:
                rules.append(PermissionRule.from_dict(item))  # type: ignore[arg-type]
            except Exception as exc:
                logger.warning(
                    "PermissionPolicy.from_dict: skipping bad rule %r: %s", item, exc
                )
        return cls(rules=rules, default_behavior=default)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "PermissionPolicy":
        """Load a policy from a JSON file.

        Expected format::

            {
              "default_behavior": "allow",
              "rules": [
                {"pattern": "write_*",    "behavior": "ask"},
                {"pattern": "bash",       "behavior": "ask"},
                {"pattern": "doom_loop",  "behavior": "deny"}
              ]
            }

        Alternatively, the file may be a bare JSON *array* of rule objects:

            [
              {"pattern": "write_*", "behavior": "ask"},
              {"pattern": "bash",    "behavior": "deny"}
            ]

        If the file does not exist, an empty (permissive) policy is returned.
        """
        if path is None:
            path = get_permissions_path()
        if not path.exists():
            logger.debug(
                "PermissionPolicy.load: %s not found; using empty policy", path
            )
            return cls()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("PermissionPolicy.load: failed to read %s: %s", path, exc)
            return cls()

        if isinstance(data, list):
            # Bare array of rules — no top-level wrapper.
            rules: List[PermissionRule] = []
            for item in data:
                try:
                    rules.append(PermissionRule.from_dict(item))
                except Exception as exc:
                    logger.warning(
                        "PermissionPolicy.load: skipping bad rule %r: %s", item, exc
                    )
            return cls(rules=rules)

        if isinstance(data, dict):
            try:
                return cls.from_dict(data)
            except Exception as exc:
                logger.warning(
                    "PermissionPolicy.load: failed to parse policy from %s: %s; using empty policy",
                    path,
                    exc,
                )
                return cls()

        logger.warning(
            "PermissionPolicy.load: unexpected JSON type %s in %s; using empty policy",
            type(data).__name__,
            path,
        )
        return cls()

    def save(self, path: Optional[Path] = None) -> None:
        """Persist this policy to *path* as JSON."""
        if path is None:
            path = get_permissions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from src.core.io_utils import atomic_write_json

            ok = atomic_write_json(path, self.to_dict(), logger=logger)
            if ok:
                logger.info(
                    "PermissionPolicy: saved %d rule(s) to %s", len(self._rules), path
                )
                return
            logger.warning(
                "PermissionPolicy: atomic_write_json returned False for %s; falling back",
                path,
            )
        except Exception:
            # Log stack trace at debug level using traceback.format_exc() to avoid exc_info kwarg
            logger.debug(
                "PermissionPolicy: atomic_write_json unavailable or failed for %s; falling back\n%s",
                path,
                traceback.format_exc(),
            )

        # Fallback: write via mkstemp -> os.replace with fsync to avoid
        # exposing partially-written JSON. As a last resort the old
        # Path.write_text behaviour is attempted (only if mkstemp fails).
        try:
            import tempfile

            fd = None
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        fd = None
                        json.dump(self.to_dict(), f, indent=2)
                        try:
                            f.flush()
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    try:
                        os.replace(tmp, str(path))
                    except Exception:
                        try:
                            shutil.move(tmp, str(path))
                        except Exception:
                            # Last-resort fallback
                            path.write_text(
                                json.dumps(self.to_dict(), indent=2), encoding="utf-8"
                            )
                except Exception:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
                    raise
            except Exception:
                try:
                    if tmp and os.path.exists(tmp):
                        os.unlink(tmp)
                except Exception:
                    pass
                raise
            logger.info(
                "PermissionPolicy: saved %d rule(s) to %s", len(self._rules), path
            )
        except Exception:
            logger.exception("PermissionPolicy: failed to write policy to %s", path)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def make_default_policy() -> PermissionPolicy:
    """Return the recommended default policy for interactive sessions.

    This is a sensible baseline: most tools are allowed; write/delete/exec
    tools prompt for confirmation; doom_loop is hard-denied.

    Users can override by providing a permissions.json file at get_permissions_path()
    (typically under the CodingAgent data directory returned by src.core.paths.get_data_dir()).
    """
    rules = [
        # Hard-deny the doom-loop guard token (Sprint B integration).
        PermissionRule(pattern=DOOM_LOOP_TOKEN, behavior=Behavior.DENY),
    ]
    return PermissionPolicy(rules=rules, default_behavior=Behavior.ALLOW)


def make_readonly_policy() -> PermissionPolicy:
    """Return a strict read-only policy (useful for explore/verification agents)."""
    rules = [
        PermissionRule(pattern="*", behavior=Behavior.ALLOW),
        # Deny all write / execution tools.
        PermissionRule(pattern="write_*", behavior=Behavior.DENY),
        PermissionRule(pattern="edit_*", behavior=Behavior.DENY),
        PermissionRule(pattern="delete_*", behavior=Behavior.DENY),
        PermissionRule(pattern="apply_*", behavior=Behavior.DENY),
        PermissionRule(pattern="generate_patch", behavior=Behavior.DENY),
        PermissionRule(pattern="bash", behavior=Behavior.DENY),
        PermissionRule(pattern="git_commit", behavior=Behavior.DENY),
        PermissionRule(pattern="delegate_task", behavior=Behavior.DENY),
        PermissionRule(pattern=DOOM_LOOP_TOKEN, behavior=Behavior.DENY),
    ]
    return PermissionPolicy(rules=rules, default_behavior=Behavior.ALLOW)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_POLICY: Optional[PermissionPolicy] = None


def get_permission_policy() -> PermissionPolicy:
    """Return the global PermissionPolicy singleton.

    On first call, attempts to load from the user permissions file returned by
    ``src.core.paths.get_permissions_path()`` (typically under the CodingAgent
    data directory). Falls back to ``make_default_policy()`` if the file is
    absent or malformed.
    """
    global _POLICY
    if _POLICY is None:
        loaded = PermissionPolicy.load()
        # If the file was absent (empty policy), use the default policy so
        # that doom_loop is always hard-denied even without user config.
        if len(loaded) == 0:
            _POLICY = make_default_policy()
        else:
            _POLICY = loaded
    return _POLICY


def reset_permission_policy(policy: Optional[PermissionPolicy] = None) -> None:
    """Replace the global singleton (for testing or runtime reconfiguration)."""
    global _POLICY
    _POLICY = policy
