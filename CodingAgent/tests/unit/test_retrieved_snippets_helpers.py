from src.core.context.retrieved_snippets import (
    build_context_controller_descriptors,
    filter_retrieved_snippets_by_budget,
)


def test_build_context_controller_descriptors_maps_snippet_fields():
    descs = build_context_controller_descriptors(
        [{"file_path": "src/a.py", "snippet": "line1\nline2"}]
    )
    assert descs == [
        {
            "path": "src/a.py",
            "content": "line1\nline2",
            "line_count": 2,
            "estimated_tokens": max(1, len("line1\nline2") // 4),
        }
    ]


def test_filter_retrieved_snippets_by_budget_keeps_only_included_paths():
    snippets = [
        {"file_path": "src/a.py", "snippet": "A"},
        {"file_path": "src/b.py", "snippet": "B"},
    ]
    filtered = filter_retrieved_snippets_by_budget(
        snippets,
        included_descriptors=[{"path": "src/b.py"}],
    )
    assert filtered == [{"file_path": "src/b.py", "snippet": "B"}]
