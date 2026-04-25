from typing import Dict, Any, Optional
import datetime
import json
import logging
import re

try:
    import yaml as _yaml
except ImportError:
    _yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract and parse a JSON object with 'name' and 'arguments' keys."""
    import re as _re

    patterns = [
        r'\{[^}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{',
    ]

    for pattern in patterns:
        match = _re.search(pattern, text)
        if match:
            start = match.start()
            depth = 0
            end = start
            for i, c in enumerate(text[start:], start):
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                parsed = json.loads(text[start:end])
                if parsed.get("name") and parsed.get("arguments"):
                    return parsed
                if parsed.get("tool") and parsed.get("arguments"):
                    return {
                        "name": parsed.get("tool"),
                        "arguments": parsed.get("arguments"),
                    }
            except Exception:
                pass
    return None


def parse_tool_block(text: str) -> Optional[Dict[str, Any]]:
    """
    Parses JSON tool blocks from markdown text.

    Primary format:
    ```json
    {"name": "tool_name", "arguments": {"arg_name": "value"}}
    ```
    """
    if not text:
        return None

    cleaned_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    cleaned_text = re.sub(
        r"<\|channel\|>thought.*?<channel\|>", "", cleaned_text, flags=re.DOTALL
    ).strip()

    _CLOSE_FENCE = r"```[ \t]*(?:\n|$)"

    # Try JSON code blocks first
    json_patterns = [
        r"```json\s*\n(.*?)\n" + _CLOSE_FENCE,
        r"```\s*\n(\{.*?\})\n" + _CLOSE_FENCE,
    ]

    for pattern in json_patterns:
        match = re.search(pattern, cleaned_text, re.DOTALL | re.IGNORECASE)
        if match:
            json_content = match.group(1).strip()
            try:
                parsed = json.loads(json_content)
                if parsed.get("name") and parsed.get("arguments"):
                    logger.debug(
                        f"parse_tool_block: JSON parse succeeded for tool '{parsed.get('name')}'"
                    )
                    return parsed
                if parsed.get("tool") and parsed.get("arguments"):
                    logger.debug(
                        f"parse_tool_block: JSON parse succeeded for tool '{parsed.get('tool')}'"
                    )
                    return {
                        "name": parsed.get("tool"),
                        "arguments": parsed.get("arguments"),
                    }
            except Exception:
                pass

    # Try inline JSON
    inline_result = _extract_json_object(cleaned_text)
    if inline_result:
        logger.debug(
            f"parse_tool_block: inline JSON succeeded for tool '{inline_result.get('name')}'"
        )
        return inline_result

    # Try YAML code blocks
    yaml_patterns = [
        r"```yaml\s*\n(.*?)\n" + _CLOSE_FENCE,
    ]

    for pattern in yaml_patterns:
        match = re.search(pattern, cleaned_text, re.DOTALL | re.IGNORECASE)
        if match:
            yaml_content = match.group(1).strip()
            # Delegate to the central YAML block parser which understands both
            # the standard {name: .., arguments: ..} shape and the compact
            # single-key format (tool_name: { ... }).  This consolidates logic
            # and avoids missing compact YAML code blocks.
            try:
                parsed = _parse_yaml_block(yaml_content)
                if parsed:
                    logger.debug(
                        f"parse_tool_block: YAML code block succeeded for tool '{parsed.get('name')}'"
                    )
                    return parsed
            except Exception:
                pass

    # Try inline YAML
    inline_yaml = _parse_inline_yaml(cleaned_text)
    if inline_yaml:
        logger.debug(
            f"parse_tool_block: inline YAML succeeded for tool '{inline_yaml.get('name')}'"
        )
        return inline_yaml

    # Try compact YAML format: tool_name:\n  key: value
    compact_yaml = _parse_yaml_block(cleaned_text)
    if compact_yaml:
        logger.debug(
            f"parse_tool_block: compact YAML succeeded for tool '{compact_yaml.get('name')}'"
        )
        return compact_yaml

    # Generic code block fallback
    generic_pattern = r"```\s*\n(.*?)\n" + _CLOSE_FENCE
    match = re.search(generic_pattern, cleaned_text, re.DOTALL | re.IGNORECASE)
    if match:
        content = match.group(1).strip()
        try:
            # Reuse the YAML block parser here as a best-effort attempt to
            # interpret the generic fenced block. This lets us support YAML
            # shaped content inside non-labeled fences as well.
            parsed = _parse_yaml_block(content)
            if parsed:
                return parsed
        except Exception:
            pass

    logger.debug(
        f"parse_tool_block: all parse methods failed. "
        f"Input length={len(text)}, cleaned length={len(cleaned_text)}"
    )

    return None


def _parse_yaml_block(yaml_content: str) -> Optional[Dict[str, Any]]:
    """Parse YAML content from a code block.

    Tries yaml.safe_load() first (handles block scalars, quoted strings,
    multi-line content) then falls back to the custom line-by-line parser
    for malformed or partial YAML that safe_load rejects.
    """
    # --- Primary path: yaml.safe_load ----------------------------------
    try:
        if _yaml is None:
            raise ImportError("yaml not available")

        def _normalize(v: Any) -> Any:
            """Convert YAML-native types (date, datetime) to strings so
            tool arguments are always JSON-serialisable primitives."""
            if isinstance(v, (datetime.date, datetime.datetime)):
                return v.isoformat()
            if isinstance(v, dict):
                return {str(k): _normalize(val) for k, val in v.items()}
            if isinstance(v, list):
                return [_normalize(i) for i in v]
            return v

        parsed = _yaml.safe_load(yaml_content)
        if not isinstance(parsed, dict):
            # P2-H: safe_load succeeded but returned a scalar or list — this is
            # not a tool call.  Return None immediately so we don't fall through
            # to the custom line-by-line parser, which would silently extract
            # garbage (e.g. treat a YAML timestamp as a tool name).
            if parsed is not None:
                return None
        if isinstance(parsed, dict):
            # Standard format: {name: "tool", arguments: {...}}
            if "name" in parsed:
                name = str(parsed["name"])
                raw_args = parsed.get("arguments") or parsed.get("args")
                if isinstance(raw_args, dict):
                    return {"name": name, "arguments": _normalize(raw_args)}
                # arguments key absent — collect remaining keys as args
                remaining = {
                    k: _normalize(v)
                    for k, v in parsed.items()
                    if k not in ("name", "arguments", "args")
                }
                return {"name": name, "arguments": remaining}

            # Compact format: {tool_name: {arg: val}} — single top-level key
            keys = [k for k in parsed if parsed[k] is not None]
            if len(keys) == 1:
                tool_name = keys[0]
                tool_args = parsed[tool_name]
                if isinstance(tool_args, dict):
                    return {"name": str(tool_name), "arguments": _normalize(tool_args)}
    except Exception:
        pass  # fall through to custom parser

    # --- Fallback: custom line-by-line parser --------------------------
    lines = yaml_content.split("\n")

    # Check for compact format: tool_name:\n  key: value
    # Find the first non-indented line - that's the tool name
    tool_name = None
    args = {}

    first_line = True
    current_key = None
    current_value_lines = []

    for line in lines:
        # Check if this is a top-level key (no indentation)
        if (
            first_line
            and line.strip()
            and not line.startswith(" ")
            and not line.startswith("\t")
        ):
            # This might be the tool name as a key
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                if val:  # Has a value on same line
                    tool_name = key
                    if val.startswith("{") or val.startswith("["):
                        try:
                            args = json.loads(val)
                        except json.JSONDecodeError:
                            args = {key: val}
                    else:
                        args = {key: val}
                else:
                    tool_name = key
            first_line = False
            continue

        first_line = False

        # Handle indented lines
        stripped = line.strip()
        if not stripped:
            continue

        # Skip the "name:" and "arguments:" keys - they're not actual tool arguments
        if stripped == "name:" or stripped == "arguments:" or stripped == "args:":
            current_key = stripped.rstrip(":").strip()
            current_value_lines = []
            continue

        if ":" in stripped:
            # Save previous key-value if any
            if (
                current_key
                and current_key not in ["name", "arguments", "args"]
                and current_value_lines
            ):
                val_str = "\n".join(current_value_lines).strip()
                if val_str:
                    if val_str.startswith("{") or val_str.startswith("["):
                        try:
                            args[current_key] = json.loads(val_str)
                        except json.JSONDecodeError:
                            args[current_key] = val_str
                    else:
                        args[current_key] = val_str

            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()

            # Skip "name" and "arguments" keys at this level
            if key == "name" or key == "arguments" or key == "args":
                if val:
                    if val.startswith("{") or val.startswith("["):
                        try:
                            args[key] = json.loads(val)
                        except json.JSONDecodeError:
                            args[key] = val
                    else:
                        args[key] = val
                current_key = key
                current_value_lines = []
                continue

            current_key = key
            current_value_lines = []

            if val:
                if val.startswith("{") or val.startswith("["):
                    try:
                        args[current_key] = json.loads(val)
                    except json.JSONDecodeError:
                        args[current_key] = val
                else:
                    args[current_key] = val
        else:
            # Continuation of previous value
            if current_key and current_key not in ["name", "arguments", "args"]:
                current_value_lines.append(stripped)

    # Save last key-value
    if (
        current_key
        and current_key not in ["name", "arguments", "args"]
        and current_value_lines
    ):
        val_str = "\n".join(current_value_lines).strip()
        if val_str:
            if val_str.startswith("{") or val_str.startswith("["):
                try:
                    args[current_key] = json.loads(val_str)
                except json.JSONDecodeError:
                    args[current_key] = val_str
            else:
                args[current_key] = val_str

    # Handle the case where tool_name is the key and args are nested
    if tool_name and args:
        # If args only contains 'name' or 'arguments', it's probably the YAML format style
        # In that case, extract the actual name and arguments
        if "name" in args:
            actual_name = args.pop("name")
            if "arguments" in args:
                actual_args = args.pop("arguments")
                return {"name": actual_name, "arguments": actual_args}
            return {"name": actual_name, "arguments": args}
        return {"name": tool_name, "arguments": args}

    # Handle case where tool_name was extracted but args are flat
    # Look for a "name" or "tool" key
    if "name" in args:
        name = args.pop("name")
        # Move 'arguments' key to 'arguments' if present
        if "arguments" in args:
            actual_args = args.pop("arguments")
            return {"name": name, "arguments": actual_args}
        return {"name": name, "arguments": args}

    if "tool" in args:
        tool = args.pop("tool")
        if "arguments" in args:
            actual_args = args.pop("arguments")
            return {"name": tool, "arguments": actual_args}
        return {"name": tool, "arguments": args}

    return None


def _parse_inline_yaml(text: str) -> Optional[Dict[str, Any]]:
    """Parse YAML-like format from plain text (not in code blocks).
    NOTE: XML format is deprecated. Only YAML format is supported."""

    # Reject XML format entirely
    if re.search(r"<tool>", text, re.IGNORECASE):
        return None

    lines = text.split("\n")

    name = None
    args = {}
    in_arguments = False
    current_key = None
    current_value_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Skip XML-style tags if present (backward compatibility)
        if stripped.startswith("<") and stripped.endswith(">"):
            continue

        # Check for name: tool_name
        if stripped.startswith("name:"):
            name = stripped[5:].strip()
            continue

        # Check for tool: tool_name (alternative)
        if stripped.startswith("tool:"):
            name = stripped[5:].strip()
            continue

        # Check for arguments: start of args block
        if stripped.startswith("arguments:") or stripped.startswith("args:"):
            in_arguments = True
            current_key = None
            continue

        # Parse key: value pairs
        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()

            if in_arguments:
                if val:
                    # Single line value
                    if val.startswith("{") or val.startswith("["):
                        try:
                            args[key] = json.loads(val)
                        except json.JSONDecodeError:
                            args[key] = val
                    else:
                        args[key] = val
                else:
                    # Multi-line value will follow
                    current_key = key
                    current_value_lines = []
            else:
                # Not in arguments block yet
                if key == "name" or key == "tool":
                    name = val
                else:
                    args[key] = val
        elif current_key and stripped:
            # Continuation of multi-line value
            current_value_lines.append(stripped)

    # Save last multi-line value
    if current_key and current_value_lines:
        val_str = "\n".join(current_value_lines).strip()
        if val_str:
            args[current_key] = val_str

    if name:
        # If arguments contain nested 'arguments' key, extract it
        if "arguments" in args:
            actual_args = args.pop("arguments")
            return {"name": name, "arguments": actual_args}
        return {"name": name, "arguments": args}

    return None
