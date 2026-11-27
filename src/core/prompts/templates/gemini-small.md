# Gemini Flash / Lite Guidance

- Call tools immediately without announcing next steps; batch independent `read_file` calls in parallel when multiple files are named.
- Avoid speculative `glob`/`list_files` calls when target filenames are already known.
