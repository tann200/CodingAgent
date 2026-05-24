import typing as _t

_LAZY_MODULES: dict[str, str] = {
    "HistoryInput": ".history_input",
    "AgentArtifact": ".artifact",
    "ProviderCard": ".cards",
    "ThinkingProcess": ".thinking",
    "StreamView": ".stream_view",
    "ConsolePanel": ".console",
    "SideBySideDiff": ".diff_viewer",
    "InlineDiff": ".diff_viewer",
    "ChatTextArea": ".chat_input",
    "FilePickerOverlay": ".file_picker",
    "SubagentProgress": ".subagent_progress",
    "BashBlock": ".bash_block",
    "TodoListWidget": ".todo_list",
    "StatusBarMixin": ".status_bar",
    "ROLE_LABELS": ".status_bar",
    "ROLE_COLORS": ".status_bar",
    "ChatDisplayMixin": ".chat_mixin",
}

__all__ = sorted(_LAZY_MODULES)


def __getattr__(name: str) -> _t.Any:
    if name in _LAZY_MODULES:
        import importlib

        mod = importlib.import_module(_LAZY_MODULES[name], __package__)
        val = getattr(mod, name)
        globals()[name] = val  # cache for subsequent access
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
