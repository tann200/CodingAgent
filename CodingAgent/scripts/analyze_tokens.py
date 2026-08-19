import math
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parents[1]))

from src.core.orchestration.agent_brain import load_system_prompt
from src.core.orchestration.orchestrator import example_registry


def estimate_tokens(text: str) -> int:
    # Basic estimation, 1 token ~ 4 chars
    return math.ceil(len(text) / 4)


def main():
    print("=== System Prompt Token Analysis ===")

    # Load different roles
    roles = [None, "strategic", "operational"]

    for role in roles:
        print(f"\n--- Role: {role or 'default (operational)'} ---")

        # Load raw system prompt
        sp = load_system_prompt(role)
        if not sp:
            print("Failed to load prompt.")
            continue

        sp_tokens = estimate_tokens(sp)
        print(f"System Prompt (Base): ~{sp_tokens} tokens ({len(sp)} chars)")

        # Add tools
        registry = example_registry()
        tools_str = "Available Tools:\n"
        for name, meta in registry.tools.items():
            tools_str += f"- {name}: {meta.get('description', '')}\n"
        tools_str += """\nTo use a tool, format your response exactly like this using YAML:

```yaml
name: tool_name
arguments:
  arg1: value
```"""

        tools_tokens = estimate_tokens(tools_str)
        print(f"Tools Block: ~{tools_tokens} tokens ({len(tools_str)} chars)")

        total_sp = sp + "\n\n" + tools_str
        total_tokens = estimate_tokens(total_sp)
        print(f"TOTAL SYSTEM MESSAGE: ~{total_tokens} tokens ({len(total_sp)} chars)")

        print("Sections length breakdown:")
        for section in [
            "<system_role>",
            "<operating_principles>",
            "<core_laws>",
            "<output_format>",
        ]:
            if section in total_sp:
                # rough extraction
                start = total_sp.find(section)
                end_tag = section.replace("<", "</")
                end = total_sp.find(end_tag) + len(end_tag)
                if end > start:
                    section_len = end - start
                    print(
                        f"  {section}: ~{estimate_tokens(total_sp[start:end])} tokens"
                    )


if __name__ == "__main__":
    main()
