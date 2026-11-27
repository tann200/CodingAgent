# Compact Local Model Guidance

- Invoke native tools directly with strictly typed arguments; zero conversational preamble.
- Copy `old_string` verbatim from `read_file` with enough surrounding lines to be unique in `edit_file_atomic`.
- On any tool error or verification feedback, read the diagnostic and apply a focused fix.
