from typing import Dict, Any, Optional
import json
import re


def parse_tool_block(text: str) -> Optional[Dict[str, Any]]:
    """
     Parses YAML tool blocks from markdown text.
     Accepts these formats:

     1. Markdown code block with yaml:
     ```yaml
     name: tool_name
     arguments:
       arg_name: value
     ```

     2. YAML frontmatter-style block:
     ```yaml
     name: tool_name
     arguments:
       arg_name: value
     ```

     3. Compact YAML format (direct key-value):
     ```yaml
     tool_name:
       arg_name: value
     ```

     4. With thinking blocks (LMStudio style):
    <think>
     The user wants me to list files...
    </think>
     ```yaml
     name: list_files
     arguments:
       path: .
     ```
    """
    if not text:
        return None

    # Strip thinking blocks first (LMStudio/Cherry Studio format)
    # These appear as ```yaml ... ``` AFTER the thinking block
    cleaned_text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Try markdown YAML/JSON code blocks (primary format)
    yaml_patterns = [
        # Pattern: ```yaml ... ``` blocks
        r"```yaml\s*\n(.*?)\n```",
        # Pattern: ```json ... ``` blocks
        r"```json\s*\n(.*?)\n```",
        # Pattern: ``` ... ``` blocks that might be YAML
        r"```\s*\n(.*?)\n```",
    ]

    for pattern in yaml_patterns:
        match = re.search(pattern, cleaned_text, re.DOTALL | re.IGNORECASE)
        if match:
            yaml_content = match.group(1).strip()
            result = _parse_yaml_block(yaml_content)
            if result:
                return result

    # Try inline YAML format: name: tool_name\narguments: {...}
    inline_result = _parse_inline_yaml(cleaned_text)
    if inline_result:
        return inline_result

    return None


def _parse_yaml_block(yaml_content: str) -> Optional[Dict[str, Any]]:
    """Parse YAML content from a code block."""
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

        # Check indentation level
        indent = len(line) - len(line.lstrip())

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
