"""Small, explicit install helpers for Skill folders and MCP YAML entries."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from skills.catalog import _load_skill


def install_skill(source: str, destination_root: Path) -> Path:
    """Install one Skill folder from disk or a GitHub tree URL."""
    if source.startswith("https://github.com/"):
        parts = [part for part in urlsplit(source).path.strip("/").split("/") if part]
        if len(parts) < 5 or parts[2] != "tree":
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
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True,
                                    text=True, timeout=10, check=True).stdout.strip()
            return _copy_skill(folder, destination_root, {
                "source_type": "github", "source_url": f"https://github.com/{'/'.join(parts)}", "git_ref": ref,
                "resolved_commit_sha": commit, "subpath": "/".join(parts[4:]),
            })
    if source.startswith(("http://", "https://")):
        raise ValueError("Only GitHub tree URLs or local Skill paths are supported")
    path = Path(source).expanduser().resolve()
    path = path.parent if path.name == "SKILL.md" else path
    return _copy_skill(path, destination_root, {"source_type": "local", "source_url": str(path)})


def _copy_skill(folder: Path, destination_root: Path, source_info: dict) -> Path:
    if not folder.is_dir():
        raise ValueError(f"Skill directory does not exist: {folder}")
    if any(path.is_symlink() for path in (folder, *folder.rglob("*"))):
        raise ValueError("Skill directory cannot contain symlinks")
    skill = _load_skill(folder / "SKILL.md", source="install")
    destination_root = destination_root.expanduser().resolve()
    if destination_root.is_relative_to(folder.resolve()):
        raise ValueError("Skill installation root cannot be inside its source")
    destination_root.mkdir(parents=True, exist_ok=True)
    with _skill_mutation(destination_root) as staging:
        return _install_staged_skill(folder, destination_root, source_info, skill.name, staging)


def _install_staged_skill(folder: Path, destination_root: Path, source_info: dict, name: str, staging: Path) -> Path:
    from agent.session_store import JsonChatSessionStore
    from entry.onboarding import _atomic_private_text

    destination = _skill_target(destination_root, name)
    if destination.exists():
        raise ValueError(f"Skill already exists: {destination}")
    staging.mkdir(mode=0o700)
    staged = staging / "replacement" / name
    shutil.copytree(folder, staged)
    _load_skill(staged / "SKILL.md", source="install")
    manifest = {
        "name": name, **source_info,
        "scope": "project" if destination_root.parent.name == ".agents" else "global",
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "content_digest": skill_content_digest(staged), "skill_format_version": 1,
    }
    manifest["manifest_digest"] = _manifest_digest(manifest)
    _atomic_private_text(staged / ".forge-skill-lock.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _atomic_private_text(staging / "operation.json", json.dumps({"name": name, "install": True}))
    try:
        staged.rename(destination)
        JsonChatSessionStore._fsync_directory(destination_root)
        _refresh_skill_index(destination_root)
        _atomic_private_text(staging / "committed", "committed\n")
    except BaseException:
        _recover_skill_update(destination_root, staging)
        raise
    _discard_skill_transaction(destination_root, staging)
    return destination


def _manifest_digest(manifest: dict) -> str:
    content = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _refresh_skill_index(root: Path) -> None:
    from entry.onboarding import _atomic_private_text

    target = (root.parent if root.parent.name == ".agents" else root) / "skills.lock.json"
    entries = []
    for folder in sorted(root.iterdir()):
        lock = folder / ".forge-skill-lock.json"
        if folder.name.startswith(".") or folder.is_symlink() or lock.is_symlink() or not lock.is_file():
            continue
        item = json.loads(lock.read_text(encoding="utf-8"))
        if item.get("name") != folder.name or item.get("manifest_digest") != _manifest_digest(item):
            raise ValueError(f"Invalid Skill manifest for {folder.name}")
        entries.append(item)
    _atomic_private_text(target, json.dumps({"version": 1, "skills": entries}, ensure_ascii=False, indent=2) + "\n")


def skill_content_digest(folder: Path) -> str:
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("Skill directory cannot contain symlinks")
        if path.is_file() and path != folder / ".forge-skill-lock.json":
            with path.open("rb") as content:
                files.append((path.relative_to(folder).as_posix(), hashlib.file_digest(content, "sha256").hexdigest()))
    return hashlib.sha256(json.dumps(files).encode("utf-8")).hexdigest()


@contextmanager
def _skill_mutation(root: Path):
    from agent.session_store import JsonChatSessionStore

    lock = root / ".forge-skill-operations.json"
    pending = root / ".forge-skill-transaction"
    if lock.with_suffix(".lock").is_symlink() or pending.is_symlink():
        raise ValueError("invalid Skill transaction path")
    with JsonChatSessionStore._session_lock(lock):
        _recover_skill_update(root, pending)
        yield pending


def _discard_skill_transaction(root: Path, pending: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=".skill-finished-", dir=root) as trash:
        pending.rename(Path(trash) / "finished")


def _recover_skill_update(root: Path, pending: Path) -> None:
    from agent.session_store import JsonChatSessionStore

    if not pending.exists():
        return
    if not pending.is_dir():
        raise ValueError("invalid Skill recovery directory")
    journal = pending / "operation.json"
    if journal.is_symlink() or (pending / "previous").is_symlink() or (pending / "committed").is_symlink():
        raise ValueError("invalid Skill recovery journal")
    if journal.exists():
        operation = json.loads(journal.read_text(encoding="utf-8"))
        target = _skill_target(root, operation["name"])
        previous = pending / "previous"
        if not (pending / "committed").exists() and (previous.is_dir() or operation.get("install") is True):
            if target.exists():
                target.rename(pending / "discarded")
            if previous.is_dir():
                previous.rename(target)
            JsonChatSessionStore._fsync_directory(root)
        _refresh_skill_index(root)
    elif (pending / "previous").exists():
        raise ValueError("Skill recovery journal is missing")
    _discard_skill_transaction(root, pending)


def _skill_target(root: Path, name: str) -> Path:
    if not isinstance(name, str):
        raise ValueError("invalid Skill name")
    target = root / name
    if not name or target.parent != root or name.startswith(".") or target.is_symlink():
        raise ValueError("Invalid Skill directory")
    return target


def managed_skill(root: Path, name: str) -> tuple[Path, dict]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Skill root does not exist")
    with _skill_mutation(root):
        return _read_managed_skill(root, name)


def _read_managed_skill(root: Path, name: str) -> tuple[Path, dict]:
    target = _skill_target(root, name)
    lock = target / ".forge-skill-lock.json"
    if lock.is_symlink() or not lock.is_file():
        raise ValueError("Skill was not installed with a source lock")
    manifest = json.loads(lock.read_text(encoding="utf-8"))
    if (manifest.get("name") != name or manifest.get("content_digest") != skill_content_digest(target)
            or manifest.get("manifest_digest") != _manifest_digest(manifest)):
        raise ValueError("Skill files differ from the installation lock")
    return target, manifest


def update_managed_skill(root: Path, name: str, *, remove: bool = False) -> dict:
    from agent.session_store import JsonChatSessionStore
    from entry.onboarding import _atomic_private_text

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Skill root does not exist")
    with _skill_mutation(root) as staging:
        target, manifest = _read_managed_skill(root, name)
        staging.mkdir(mode=0o700)
        replacement = None if remove else install_skill(manifest["source_url"], staging / "new")
        if replacement is not None and replacement.name != name:
            raise ValueError("Updated source changed the Skill name")
        if replacement is not None:
            replacement_lock = replacement / ".forge-skill-lock.json"
            updated = json.loads(replacement_lock.read_text(encoding="utf-8"))
            updated["scope"] = manifest["scope"]
            updated["manifest_digest"] = _manifest_digest(updated)
            replacement_lock.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _atomic_private_text(staging / "operation.json", json.dumps({"name": name}))
        try:
            target.rename(staging / "previous")
            JsonChatSessionStore._fsync_directory(staging)
            JsonChatSessionStore._fsync_directory(root)
            if replacement is not None:
                replacement.rename(target)
            JsonChatSessionStore._fsync_directory(root)
            _refresh_skill_index(root)
            _atomic_private_text(staging / "committed", "committed\n")
        except BaseException:
            _recover_skill_update(root, staging)
            raise
        _discard_skill_transaction(root, staging)
        return manifest if remove else _read_managed_skill(root, name)[1]


def change_mcp_server(config_path: Path, server_id: str, operation: str) -> None:
    from config.schema import _parse
    from entry.onboarding import _atomic_private_text

    original = config_path.read_bytes().decode("utf-8")
    raw = yaml.safe_load(original)
    if not isinstance(raw, dict) or not isinstance(raw.get("mcp"), dict):
        raise ValueError("MCP server list does not exist")
    servers = raw["mcp"].get("servers", [])
    if not isinstance(servers, list) or not all(isinstance(server, dict) for server in servers):
        raise ValueError("mcp.servers must be a list of mappings")
    matches = [index for index, server in enumerate(servers) if server.get("id") == server_id]
    if len(matches) != 1:
        raise ValueError("MCP server does not exist or is ambiguous")
    index = matches[0]
    doc = yaml.compose(original)

    def field(mapping, key_name):
        return next((value for key, value in mapping.value if key.value == key_name), None)

    sequence = field(field(doc, "mcp"), "servers")
    item = sequence.value[index]
    if sequence.flow_style or item.flow_style:
        raise ValueError("MCP changes require a block list of server mappings")
    newline = "\r\n" if "\r\n" in original else "\n"

    def line_start(offset: int) -> int:
        return original.rfind("\n", 0, offset) + 1

    def line_end(offset: int) -> int:
        eol = original.find("\n", offset)
        return len(original) if eol == -1 else eol + 1

    if operation == "remove":
        servers.pop(index)
        start = line_start(item.start_mark.index)
        last_value = item.value[-1][1]
        end_mark = last_value.end_mark.index
        if isinstance(last_value, yaml.ScalarNode) or getattr(last_value, "flow_style", False):
            end = line_end(end_mark)
        else:
            end = end_mark if end_mark == len(original) else line_start(end_mark)
        if not servers:
            indent = " " * sequence.start_mark.column
            replacement = f"{indent}[]{newline}"
        else:
            replacement = ""
        updated = original[:start] + replacement + original[end:]
    elif operation in {"enable", "disable"}:
        enabled = operation == "enable"
        servers[index]["enabled"] = enabled
        value = "true" if enabled else "false"
        node = field(item, "enabled")
        if node is None:
            start = line_end(item.value[0][1].end_mark.index)
            separator = "" if original[:start].endswith("\n") else newline
            line = separator + " " * item.start_mark.column + f"enabled: {value}" + newline
            updated = original[:start] + line + original[start:]
        else:
            updated = original[:node.start_mark.index] + value + original[node.end_mark.index:]
    else:
        raise ValueError("Unknown MCP configuration change")
    _parse(raw)
    if yaml.safe_load(updated) != raw:
        raise ValueError("MCP edit could not preserve unrelated YAML")
    _atomic_private_text(config_path, updated)


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
