# Skill Name: The DRY Principle (Don't Repeat Yourself)

## When to Use
This skill should be applied when encountering duplicate code blocks, repetitive logic, or opportunities for abstraction. It's essential for improving code maintainability, reducing bugs, and enhancing readability.

## Strategy
The core strategy involves proactively identifying and eliminating redundancy by consolidating similar code into a single, reusable component. This requires careful analysis to ensure that abstractions are appropriate and do not lead to over-engineering.

## Execution Steps
1.  **Redundancy Detection:**
    *   Before implementing new complex functions or classes, use structural search tools (e.g., `grep`) to check for existing similar logic.
    *   Prioritize searching common utility folders (`src/utils`, `helpers`, `lib`) for existing abstractions before creating new ones.
2.  **Scope Adherence (The Sandbox Rule):**
    *   **Local Duplication:** If duplication is found *within* the current target file, extract it into a local helper function or class within that file.
    *   **Cross-file Duplication (Architects Only):** If duplication is detected across multiple files and a global abstraction is necessary, **do not attempt a rogue refactor.** Instead, document the duplication and, if acting as an Architect, plan a sequence of isolated tasks: e.g., Task 1 (Create utility), Task 2-N (Update call-sites).
3.  **Avoid Hasty Abstractions (AHA):**
    *   Only consolidate code if the logic genuinely represents the same business domain or core functionality. Avoid abstracting merely "similar-looking" code if their underlying purposes differ, as this can lead to over-engineering and increased complexity.
