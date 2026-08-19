from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class SlashCommand:
    name: str
    description: str
    handler: Callable[..., Awaitable[None] | None]
    source: str = "builtin"
    args_example: str = ""


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        key = cmd.name.lower().strip()
        self._commands[key] = cmd

    def unregister(self, name: str) -> None:
        self._commands.pop(name.lower().strip(), None)

    def get(self, name: str) -> SlashCommand | None:
        return self._commands.get(name.lower().strip())

    async def dispatch(self, app: Any, text: str) -> bool:
        text = text.lstrip("/")
        parts = text.split(None, 1)
        raw_cmd = parts[0].lower().strip() if parts else ""
        raw_args = parts[1].strip() if len(parts) > 1 else ""
        cmd = self.get(raw_cmd)
        if cmd is None:
            return False
        result = cmd.handler(app, raw_args)
        if inspect.iscoroutine(result):
            await result
        return True

    def help_text(self) -> str:
        lines = ["Available commands:"]
        if self._commands:
            indent = len(max(self._commands.keys(), key=len)) + 2
        else:
            indent = 0
        for name in sorted(self._commands):
            cmd = self._commands[name]
            padded = name.ljust(indent)
            example = f" {cmd.args_example}" if cmd.args_example else ""
            lines.append(f"  /{padded}{example}  — {cmd.description}")
        return "\n".join(lines)

    def keys(self) -> list[str]:
        return list(self._commands.keys())

    def __contains__(self, name: str) -> bool:
        return name.lower().strip() in self._commands

    def __len__(self) -> int:
        return len(self._commands)
