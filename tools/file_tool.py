"""
tools/file_tool.py

文件操作工具，提供四个 action：
- file_read:   读取文件全部内容
- file_view:   分窗口查看文件（防止一次读爆上下文）
- file_edit:   唯一 exact replacement，保留未修改字节与行尾格式
- file_write:  写入文件（全量覆盖）

设计原则：
- file_read 对大文件做行数截断，超出时提示用 file_view 分页
- file_view 维护"窗口"概念，每次返回固定行数，agent 可 scroll
- file_write 写入前自动创建父目录，写入后返回行数确认
- 可选 workspace 边界：入口传入 repo/worktree 路径后，文件读写都限制在该目录内
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolEffect, ToolErrorType, ToolResult


# 单次 file_read 最多返回的行数，超出提示用 file_view
MAX_READ_LINES = 500
# file_view 每窗口显示的行数
VIEW_WINDOW_LINES = 100

_INTERNAL_SKILLS_HINT = (
    "Agent Skills content is runtime-owned. Use skill_load / "
    "skill_reference_load instead of generic file tools."
)


class FileReadTool(BaseTool):
    """
    读取文件内容。超过 MAX_READ_LINES 行时截断并提示。

    params:
        path (str): 文件路径（相对或绝对）
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._workspace = Path(workspace).resolve() if workspace else None

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect.READ_ONLY

    @property
    def name(self) -> str:
        return "file_read"

    @property
    def description(self) -> str:
        return (
            f"Read the contents of a file. "
            f"Files longer than {MAX_READ_LINES} lines will be truncated; "
            f"use file_view with line numbers to read specific sections."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (absolute or relative to repo root)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        resolved = _resolve_workspace_path(params.get("path", ""), self._workspace)
        if isinstance(resolved, ToolResult):
            return resolved
        path = resolved
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )
        if not path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a file: {path}",
            )

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        total = len(lines)
        truncated = total > MAX_READ_LINES
        display_lines = lines[:MAX_READ_LINES]

        # 加行号，方便 agent 用 file_view 定位
        numbered = "\n".join(
            f"{i + 1:4d} | {line}"
            for i, line in enumerate(display_lines)
        )

        suffix = ""
        if truncated:
            suffix = (
                f"\n... ({total - MAX_READ_LINES} more lines not shown) "
                f"Use file_view with start_line to read the rest."
            )

        return ToolResult(
            success=True,
            output=f"File: {path} ({total} lines total)\n{numbered}{suffix}",
        )


class FileViewTool(BaseTool):
    """
    分窗口查看文件，每次返回 VIEW_WINDOW_LINES 行。

    params:
        path (str):       文件路径
        start_line (int): 从第几行开始（1-indexed，默认 1）
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._workspace = Path(workspace).resolve() if workspace else None

    @property
    def effect(self) -> ToolEffect:
        return ToolEffect.READ_ONLY

    @property
    def name(self) -> str:
        return "file_view"

    @property
    def description(self) -> str:
        return (
            f"View a specific section of a file, {VIEW_WINDOW_LINES} lines at a time. "
            f"Use start_line to scroll through large files. Lines are 1-indexed."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "start_line": {
                    "type": "integer",
                    "description": f"First line to show (1-indexed, default 1)",
                },
            },
            "required": ["path"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        resolved = _resolve_workspace_path(params.get("path", ""), self._workspace)
        if isinstance(resolved, ToolResult):
            return resolved
        path = resolved
        start_line = max(1, int(params.get("start_line", 1)))

        if not path.exists():
            return ToolResult(success=False, output="", error=f"File not found: {path}")
        if not path.is_file():
            return ToolResult(success=False, output="", error=f"Not a file: {path}")

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        total = len(lines)
        if start_line > total:
            return ToolResult(
                success=False,
                output="",
                error=f"start_line {start_line} exceeds file length ({total} lines)",
            )

        end_line = min(start_line + VIEW_WINDOW_LINES - 1, total)
        window = lines[start_line - 1 : end_line]

        numbered = "\n".join(
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(window)
        )

        nav = ""
        if end_line < total:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. Next: file_view path={path} start_line={end_line + 1}]"
        else:
            nav = f"\n[Lines {start_line}–{end_line} of {total}. End of file.]"

        return ToolResult(success=True, output=numbered + nav)


class FileEditTool(BaseTool):
    """
    对已有文件做一次唯一 exact replacement。

    params:
        path (str):     文件路径
        old_text (str): 必须在文件中恰好出现一次的原文本
        new_text (str): 替换后的文本

    使用原始 bytes 做 UTF-8 exact replacement，只改命中的字节区间，
    因此不会把未修改区域的 CRLF/LF、EOF newline 或 BOM 重新序列化。
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._workspace = Path(workspace).resolve() if workspace else None

    @property
    def name(self) -> str:
        return "file_edit"

    @property
    def description(self) -> str:
        return (
            "Edit an existing file by replacing one exact, unique text occurrence. "
            "Prefer this for localized changes because bytes outside the replacement "
            "are preserved, including line endings and trailing-newline state. "
            "The call fails if old_text is missing or occurs more than once."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the existing file to edit",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text that must occur exactly once",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text",
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        resolved = _resolve_workspace_path(params.get("path", ""), self._workspace)
        if isinstance(resolved, ToolResult):
            return resolved
        path = resolved
        old_text = params.get("old_text", "")
        new_text = params.get("new_text", "")

        if not old_text:
            return ToolResult(
                success=False,
                output="",
                error="old_text must not be empty",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )
        if not path.exists():
            return ToolResult(
                success=False,
                output="",
                error=f"File not found: {path}",
            )
        if not path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"Not a file: {path}",
            )

        old_bytes = old_text.encode("utf-8")
        new_bytes = new_text.encode("utf-8")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        occurrences = raw.count(old_bytes)
        if occurrences == 0:
            return ToolResult(
                success=False,
                output="",
                error="old_text was not found in the target file",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )
        if occurrences != 1:
            return ToolResult(
                success=False,
                output="",
                error=f"old_text must be unique; found {occurrences} occurrences",
                error_type=ToolErrorType.INVALID_ARGUMENTS,
            )

        updated = raw.replace(old_bytes, new_bytes, 1)
        try:
            path.write_bytes(updated)
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        return ToolResult(
            success=True,
            output=(
                f"Replaced 1 exact occurrence in {path}; "
                "bytes outside the replacement were preserved."
            ),
        )


class FileWriteTool(BaseTool):
    """
    写入文件（全量覆盖）。自动创建父目录。

    params:
        path (str):    文件路径
        content (str): 要写入的内容
    """

    def __init__(self, workspace: str | Path | None = None) -> None:
        self._workspace = Path(workspace).resolve() if workspace else None

    @property
    def name(self) -> str:
        return "file_write"

    @property
    def description(self) -> str:
        return (
            "Write content to a file, replacing its entire contents. "
            "Parent directories are created automatically. "
            "Always read the file first before writing to avoid losing existing content."
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    def execute(self, params: dict[str, Any]) -> ToolResult:
        resolved = _resolve_workspace_path(params.get("path", ""), self._workspace)
        if isinstance(resolved, ToolResult):
            return resolved
        path = resolved
        content = params.get("content", "")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return ToolResult(success=False, output="", error=str(e))

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(
            success=True,
            output=f"Written {line_count} lines to {path}",
        )


def _resolve_workspace_path(
    raw_path: str | Path,
    workspace: Path | None,
) -> Path | ToolResult:
    """Resolve a tool path and reject paths outside workspace when configured."""
    if not raw_path:
        return ToolResult(success=False, output="", error="path is required")

    path = Path(raw_path)
    try:
        if workspace is None:
            if _is_internal_skill_path(path):
                return ToolResult(
                    success=False,
                    output="",
                    error=_INTERNAL_SKILLS_HINT,
                )
            return path

        target = (path if path.is_absolute() else workspace / path).resolve()
        if os.path.commonpath([str(workspace), str(target)]) != str(workspace):
            return ToolResult(
                success=False,
                output="",
                error=f"Path escapes workspace: {raw_path}",
            )
        try:
            relative = target.relative_to(workspace)
        except ValueError:
            relative = target
        if _is_internal_skill_path(relative):
            return ToolResult(
                success=False,
                output="",
                error=_INTERNAL_SKILLS_HINT,
            )
        return target
    except (OSError, ValueError) as exc:
        return ToolResult(
            success=False,
            output="",
            error=f"Invalid path {raw_path!r}: {exc}",
        )


def _is_internal_skill_path(path: Path) -> bool:
    parts = tuple(part.lower() for part in path.parts)
    return any(
        parts[index] == ".agents" and parts[index + 1] == "skills"
        for index in range(len(parts) - 1)
    )
