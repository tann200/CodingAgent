# Standard Local Model Guidance

- Batch up to 2 concurrent read-only calls (`read_file`, `find_files`, `grep`); execute mutations sequentially.
- Copy `old_string` verbatim with unique context lines in `edit_file_atomic`.
- On verification feedback, fix only the reported failing lines in place.
