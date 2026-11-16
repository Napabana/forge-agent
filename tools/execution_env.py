"""不可信子进程的最小环境；不是 OS/同用户进程隔离。"""
from __future__ import annotations

import os
from collections.abc import Mapping


# 不传播 PYTHONPATH、启动钩子、代理认证、Git 配置和任意 Provider 凭据。
_ALLOWED = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
    "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "VIRTUAL_ENV",
    "TERM", "COLUMNS", "LINES",
})


def execution_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    result = {key: value for key, value in source.items() if key.upper() in _ALLOWED}
    result.update(PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0")
    return result
