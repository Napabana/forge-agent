"""Small, explicit install helpers for Skill folders and MCP YAML entries."""

from __future__ import annotations

import copy
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from skills.catalog import _load_skill


def install_skill(source: str, destination_root: Path) -> Path:
    """Install one Skill folder from disk or a GitHub tree URL."""
    if source.startswith("https://github.com/"):
        parts = [part for part in urlsplit(source).path.strip("/").split("/") if part]
        if len(parts) < 6 or parts[2] != "tree":
            raise ValueError("GitHub source must be a /OWNER/REPO/tree/REF/PATH URL")
        owner, repo, _, ref = parts[:4]
        if not all(re.fullmatch(r"[A-Za-z0-9._-]+", value) for value in (owner, repo, ref)):
            raise ValueError("invalid GitHub owner, repository, or ref")
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            try:
                completed = subprocess.run(
                    ["git", "clone", "--quiet", "--depth", "1", "--branch", ref,
                     f"https://github.com/{owner}/{repo}.git", str(checkout)],
                    capture_output=True, text=True, timeout=90, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ValueError("GitHub clone timed out") from exc
            if completed.returncode:
                raise ValueError(f"GitHub clone failed: {completed.stderr.strip()[:300]}")
            folder = checkout.joinpath(*parts[4:]).resolve()
            if not folder.is_relative_to(checkout.resolve()):
                raise ValueError("Skill path escapes the GitHub checkout")
            return _copy_skill(folder, destination_root)
    if source.startswith(("http://", "https://")):
        raise ValueError("Only GitHub tree URLs or local Skill paths are supported")
    path = Path(source).expanduser().resolve()
    return _copy_skill(path.parent if path.name == "SKILL.md" else path, destination_root)


def _copy_skill(folder: Path, destination_root: Path) -> Path:
    if not folder.is_dir():
        raise ValueError(f"Skill directory does not exist: {folder}")
    if any(path.is_symlink() for path in (folder, *folder.rglob("*"))):
        raise ValueError("Skill directory cannot contain symlinks")
    skill = _load_skill(folder / "SKILL.md", source="install")
    destination_root = destination_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / skill.name
    if destination.exists():
        raise ValueError(f"Skill already exists: {destination}")
    with tempfile.TemporaryDirectory(dir=destination_root, prefix=".skill-") as temporary:
        staged = Path(temporary) / skill.name
        shutil.copytree(folder, staged)
        _load_skill(staged / "SKILL.md", source="install")
        staged.rename(destination)
    return destination


def add_mcp_server(config_path: Path, server: dict, *, default_template: Path | None = None) -> Path:
    """Insert one server into config.yaml without rewriting unrelated YAML."""
    config_path = config_path.expanduser()
    if config_path.exists():
        original = config_path.read_bytes().decode("utf-8")
        mode = stat.S_IMODE(config_path.stat().st_mode)
    elif default_template is not None:
        original = default_template.read_bytes().decode("utf-8")
        mode = 0o600
    else:
        raise ValueError(f"Config file does not exist: {config_path}")
    parsed = yaml.safe_load(original) or {}
    if not isinstance(parsed, dict):
        raise ValueError("config.yaml must contain a YAML mapping")
    mcp = parsed.get("mcp", {})
    if not isinstance(mcp, dict) or not isinstance(mcp.get("servers", []), list):
        raise ValueError("mcp.servers must be a list")
    if any(item.get("id") == server["id"] for item in mcp.get("servers", []) if isinstance(item, dict)):
        raise ValueError(f"MCP server already exists: {server['id']}")

    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    item = ["    " + line + newline for line in yaml.safe_dump(
        [server], sort_keys=False, allow_unicode=True,
    ).splitlines()]
    mcp_index = next((i for i, line in enumerate(lines) if re.fullmatch(
        r"mcp:\s*(?:#.*)?", line.rstrip("\r\n"))), None)
    if mcp_index is None:
        if "mcp" in parsed:
            raise ValueError("mcp must use a block-style mapping")
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines += ["mcp:" + newline, "  enabled: false" + newline, "  servers:" + newline, *item]
    else:
        block_end = next((i for i in range(mcp_index + 1, len(lines))
                          if lines[i].strip() and not lines[i].lstrip().startswith("#")
                          and not lines[i][0].isspace()), len(lines))
        servers_index = next((i for i in range(mcp_index + 1, block_end)
                              if re.match(r"^  servers:\s*", lines[i])), None)
        if servers_index is None:
            lines[block_end:block_end] = ["  servers:" + newline, *item]
        else:
            value = lines[servers_index].split(":", 1)[1].strip()
            if value.startswith("[]"):
                comment = value[2:].strip()
                lines[servers_index:servers_index + 1] = [
                    "  servers:" + (" " + comment if comment.startswith("#") else "") + newline,
                    *item,
                ]
            elif not value or value.startswith("#"):
                insert_at = next((i for i in range(servers_index + 1, block_end)
                                  if lines[i].strip() and not lines[i].lstrip().startswith("#")
                                  and len(lines[i]) - len(lines[i].lstrip()) <= 2), block_end)
                lines[insert_at:insert_at] = item
            else:
                raise ValueError("mcp.servers must use a block list or []")

    updated = "".join(lines)
    expected = copy.deepcopy(parsed)
    if "mcp" not in expected:
        expected["mcp"] = {"enabled": False}
    expected["mcp"].setdefault("servers", []).append(server)
    if yaml.safe_load(updated) != expected:
        raise ValueError("MCP insertion changed unrelated YAML values")
    from config.schema import _parse
    _parse(expected)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=config_path.parent,
                                     prefix=".config-", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(updated)
    try:
        os.chmod(temporary, mode)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return config_path
