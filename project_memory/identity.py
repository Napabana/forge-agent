"""Stable, credential-free project identity."""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def normalize_remote(raw: str) -> str:
    raw = raw.strip()
    if re.match(r"^(?:[^@/]+@)?[^:/]+:.+", raw) and "://" not in raw:
        host, path = raw.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        path = path.split("?", 1)[0].split("#", 1)[0]
    else:
        url = urlsplit(raw)
        host, path = url.hostname or "", url.path
    if not host or not path:
        raise ValueError("invalid Git remote")
    return f"{host.lower()}/{path.strip('/').removesuffix('.git').lower()}"


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    source: str
    key: str


class ProjectIdentityResolver:
    @staticmethod
    def resolve(repo_path: str | Path) -> ProjectIdentity:
        root = Path(repo_path).expanduser().resolve()

        def git(*args: str) -> str | None:
            try:
                result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                        text=True, timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
            return result.stdout.strip() if result.returncode == 0 else None

        remote = git("config", "--get", "remote.origin.url") if git("rev-parse", "--is-inside-work-tree") == "true" else None
        if remote:
            try:
                source, key = "remote", normalize_remote(remote)
            except ValueError:
                remote = None
        if not remote:
            common = git("rev-parse", "--git-common-dir")
            if common:
                source, key = "git_common_dir", str((root / common).resolve())
            else:
                source, key = "path", str(root)
        return ProjectIdentity(hashlib.sha256(f"{source}:{key}".encode()).hexdigest(), source, key)
