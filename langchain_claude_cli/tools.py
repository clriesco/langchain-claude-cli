"""Claude Code built-in tool names and access presets."""

from __future__ import annotations

from enum import Enum


class ClaudeTool(str, Enum):
    """Claude Code built-in tools (Claude Code CLI 2.x)."""

    ASK_USER_QUESTION = "AskUserQuestion"
    BASH = "Bash"
    BASH_OUTPUT = "BashOutput"
    EDIT = "Edit"
    EXIT_PLAN_MODE = "ExitPlanMode"
    GLOB = "Glob"
    GREP = "Grep"
    KILL_SHELL = "KillShell"
    NOTEBOOK_EDIT = "NotebookEdit"
    READ = "Read"
    SKILL = "Skill"
    SLASH_COMMAND = "SlashCommand"
    TASK = "Task"
    TODO_WRITE = "TodoWrite"
    WEB_FETCH = "WebFetch"
    WEB_SEARCH = "WebSearch"
    WRITE = "Write"


READ_ONLY_TOOLS = [ClaudeTool.READ, ClaudeTool.GLOB, ClaudeTool.GREP]
WRITE_TOOLS = [ClaudeTool.EDIT, ClaudeTool.WRITE]
NETWORK_TOOLS = [ClaudeTool.WEB_FETCH, ClaudeTool.WEB_SEARCH]
SHELL_TOOLS = [ClaudeTool.BASH, ClaudeTool.BASH_OUTPUT, ClaudeTool.KILL_SHELL]
ALL_TOOLS = list(ClaudeTool)


def normalize_tools(tools: list[str | ClaudeTool]) -> list[str]:
    """Normalize tool names/enums to unique string names, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for t in tools:
        name = t.value if isinstance(t, ClaudeTool) else str(t)
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result
