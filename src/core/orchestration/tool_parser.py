from typing import Dict, Any, Optional
import json
import logging
import re
import yaml

logger = logging.getLogger(__name__)


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract and parse a JSON object with 'name' and 'arguments' keys."""
    patterns = [
        r'\{[^}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{',
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
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

    cleaned_text = re.sub(r"", "", text, flags=re.DOTALL).strip()
    cleaned_text = re.sub(
        r"<\|channel\|>thought.*?<channel\|>", "", cleaned_text, flags=re.DOTALL
    ).strip()

    _CLOSE_FENCE = r"```[ \t]*(?:\n|$)"

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

    yaml_patterns = [
        r"```yaml\s*\n(.*?)" + _CLOSE_FENCE,
    ]

    for pattern in yaml_patterns:
        match = re.search(pattern, cleaned_text, re.DOTALL | re.IGNORECASE)
        if match:
            yaml_content = match.group(1).strip()
            try:
                parsed = yaml.safe_load(yaml_content)
                if isinstance(parsed, dict):
                    if parsed.get("name") and parsed.get("arguments"):
                        args = parsed["arguments"]
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                pass
                        if isinstance(args, dict):
                            logger.debug(
                                f"parse_tool_block: YAML parse succeeded for tool '{parsed['name']}'"
                            )
                            return {"name": parsed["name"], "arguments": args}
                    # Compact format: tool name as the top-level key
                    for key in parsed:
                        if isinstance(parsed[key], dict) and "path" in parsed[key]:
                            logger.debug(
                                f"parse_tool_block: YAML compact format for tool '{key}'"
                            )
                            return {"name": key, "arguments": parsed[key]}
            except Exception:
                pass

    # Inline YAML: bare YAML not wrapped in code fences
    try:
        inline_parsed = yaml.safe_load(cleaned_text)
        if isinstance(inline_parsed, dict):
            if inline_parsed.get("name") and inline_parsed.get("arguments"):
                args = inline_parsed["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                if isinstance(args, dict):
                    logger.debug(
                        f"parse_tool_block: inline YAML succeeded for tool '{inline_parsed['name']}'"
                    )
                    return {"name": inline_parsed["name"], "arguments": args}
    except Exception:
        pass

    inline_result = _extract_json_object(cleaned_text)
    if inline_result:
        logger.debug(
            f"parse_tool_block: inline JSON succeeded for tool '{inline_result.get('name')}'"
        )
        return inline_result

    xml_result = _parse_qwen3_xml(cleaned_text)
    if xml_result:
        logger.debug(
            f"parse_tool_block: Qwen3 XML succeeded for tool '{xml_result.get('name')}'"
        )
        return xml_result

    logger.debug(
        f"parse_tool_block: all parse methods failed. "
        f"Input length={len(text)}, cleaned length={len(cleaned_text)}"
    )

    return None


def _parse_qwen3_xml(text: str) -> Optional[Dict[str, Any]]:
    """Parse Qwen3 native XML tool call format.

    Qwen3 can emit tool calls in XML format:
    ```
    <tool_call>
      <name>tool_name</name>
      <arguments>
      {"arg1": "value1", "arg2": "value2"}
      </arguments>
    </tool_call>
    ```

    This parser extracts the tool name and arguments, converting arguments
    to JSON if they're a JSON string.
    """
    name_pattern = r"<name>\s*([A-Za-z_]\w*)\s*</name>"
    args_pattern = r"<arguments>\s*(.*?)\s*</arguments>"

    name_match = re.search(name_pattern, text, re.DOTALL)
    args_match = re.search(args_pattern, text, re.DOTALL)

    if not name_match:
        return None

    tool_name = name_match.group(1)

    if args_match:
        args_str = args_match.group(1).strip()
        try:
            args = json.loads(args_str)
            if isinstance(args, dict):
                return {"name": tool_name, "arguments": args}
        except Exception:
            pass

    return {"name": tool_name, "arguments": {}}