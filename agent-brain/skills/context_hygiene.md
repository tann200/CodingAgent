# Skill Name: Context Hygiene & Anti-Rot

## When to Use
This skill should be applied when dealing with long sessions, potential token exhaustion, context degradation, or when the agent feels "stuck" or is performing slowly due to a cluttered context window. It's crucial for maintaining high-precision performance in memory-constrained environments.

## Strategy
The core strategy involves proactive management of the agent's context to prevent "context rot" and ensure efficient token usage. This means prioritizing critical information, externalizing persistent memory, and using search tools judiciously to fetch only necessary context.

## Execution Steps
1.  **Forced Amnesia Acknowledgment:** Understand that conversational history is volatile. Any crucial architectural decisions or context must be explicitly written to persistent memory files (e.g., `.agent-context/memory.md` or `.agent-context/DECISIONS.md`).
2.  **Search-First Mandate:**
    *   **Avoid guessing file paths:** Always use directory listing tools (e.g., `list_files`) to ascertain file locations.
    *   **Targeted Reads:** Never read files blindly. Utilize structural search tools (e.g., `grep`) to pinpoint exact symbols or line numbers before reading a file to avoid dumping large, irrelevant content into the context.
    *   **Atomic Context Gating:** Only load specific, relevant sections of a file into the context window when a change is needed, rather than the entire file.
3.  **Token Efficiency:**
    *   **Prioritize Tool Calls:** Use deterministic tool calls as the primary mode of interaction, as they are more token-efficient than lengthy reasoning.
    *   **Concise Responses:** Avoid repeating user prompts or verbose explanations. Get straight to the execution or necessary information.
