"""Shared repository fingerprint used by Context and Chat lifecycle code."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent.loop_detector import snapshot_repository


def repository_fingerprint(repo_path: str | Path) -> str:
    """返回 HEAD + working-tree snapshot 的稳定仓库指纹。

    HEAD 用于区分提交变化；snapshot_repository 覆盖暂存、未暂存、未跟踪
    文件，以及非 Git 目录中的文件状态。该函数只读，不修改仓库。
    """
    root = Path(repo_path)
    head = "no-head"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            head = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return f"{head}:{snapshot_repository(root)}"
