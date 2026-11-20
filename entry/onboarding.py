"""User configuration bootstrap and local environment checks."""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import click
import yaml

from llm.router import create_backend_from_config
from llm.base import LLMMessage


def _atomic_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            if os.name == "nt":
                from durable_runtime.windows import restrict_access

                restrict_access(Path(temp))
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(temp).unlink(missing_ok=True)


def initialize(config_path: Path, *, provider: str, model: str, protocol: str | None,
               base_url: str | None, key_env: str | None, key: str | None, force: bool,
               context_window: int | None = None, max_output_tokens: int | None = None,
               use_keyring: bool = False) -> None:
    from config.credentials import PREFIX, keyring_credential

    if config_path.exists() and not force:
        raise click.ClickException("Configuration already exists. Use --force to update it.")
    if not provider.strip() or not model.strip():
        raise click.ClickException("Provider and model are required.")
    if key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise click.ClickException("Invalid credential environment variable name.")
    if key is not None and (not key or key[0] in "'\"" or key[-1] in "'\""
                            or any(character.isspace() or character == "\0" for character in key)):
        raise click.ClickException("Invalid credential format.")
    if use_keyring and (key_env is not None or key is None):
        raise click.ClickException("Keyring storage requires an interactive API key.")
    if base_url:
        endpoint = urlsplit(base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise click.ClickException("Use an HTTP(S) endpoint without credentials, query or fragment.")
    credential = key if key is not None else (os.environ.get(key_env, "") if key_env else None)
    if key_env and not credential:
        raise click.ClickException(f"Credential environment variable {key_env} is empty.")
    old = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    if old is None:
        old = {}
    if not isinstance(old, dict) or not isinstance(old.get("llm", {}), dict):
        raise click.ClickException("Existing configuration must contain a YAML mapping.")
    selected_protocol = protocol or ("messages" if provider == "anthropic" else "chat_completions")
    if key is not None and not use_keyring:
        key_env = "FORGE_LLM_API_KEY_" + uuid.uuid4().hex[:12].upper()
    reference = PREFIX + uuid.uuid4().hex if use_keyring else "${" + key_env + "}" if key_env else ""
    old["llm"] = {**old.get("llm", {}), "provider": provider, "model": model, "protocol": selected_protocol,
                  "base_url": base_url or "", "api_key": reference}
    if context_window is not None:
        old["llm"]["context_window"] = context_window
    if max_output_tokens is not None:
        old["llm"]["max_output_tokens"] = max_output_tokens
    from config.schema import _parse

    _parse(old)
    try:
        backend = create_backend_from_config({"provider": provider, "model": model, "protocol": selected_protocol,
                                              "base_url": base_url, "api_key": credential, "max_tokens": 16})
        backend.complete([LLMMessage("user", "Reply with OK.")], [])
    except Exception as exc:
        raise click.ClickException(f"Provider validation failed ({type(exc).__name__}).") from None
    if use_keyring:
        keyring_credential(reference, password=key)
    elif key is not None:
        env_path = Path(os.environ.get("FORGE_ENV_FILE") or config_path.parent / "env").expanduser()
        previous = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if previous and not previous.endswith("\n"):
            previous += "\n"
        _atomic_private_text(env_path, f"{previous}{key_env}={key}\n")
    try:
        _atomic_private_text(config_path, yaml.safe_dump(old, allow_unicode=True, sort_keys=False))
    except BaseException:
        if use_keyring:
            keyring_credential(reference, delete=True)
        raise


def environment_summary(config, config_path: Path, repo: Path, *, check_model: bool = False) -> dict:
    from durable_runtime.store import DurableRuntimeStore, StoreFormatError
    from durable_runtime.workspace_trust import read_trust
    from project_memory.identity import ProjectIdentityResolver

    def available(command: list[str]) -> bool:
        try:
            result = subprocess.run(command, cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=5, check=False)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    identity = ProjectIdentityResolver.resolve(repo)
    settings = config.durable_runtime
    runtime_root = Path(settings.root).expanduser() if settings.root else Path.home() / ".forge-agent" / "runtime"
    runtime_path = runtime_root / identity.project_id / "runtime.db"
    runtime_status = "not_created"
    trust = []
    if runtime_path.is_file():
        try:
            with DurableRuntimeStore(identity, settings.root, timeout_ms=settings.busy_timeout_ms) as store:
                store.integrity_check()
                trust = sorted(read_trust(store))
                runtime_status = "ok"
        except (StoreFormatError, OSError, ValueError):
            runtime_status = "failed"
    model_status = "not_checked"
    if check_model:
        try:
            backend = create_backend_from_config({
                "provider": config.llm.provider, "model": config.llm.model, "protocol": config.llm.protocol,
                "base_url": config.llm.base_url or None, "api_key": config.llm.api_key or None, "max_tokens": 16,
            })
            backend.complete([LLMMessage("user", "Reply with OK.")], [])
            model_status = "ok"
        except Exception as exc:
            model_status = type(exc).__name__
    return {
        "config_path": str(config_path), "config_exists": config_path.is_file(),
        "provider": config.llm.provider, "model": config.llm.model, "protocol": config.llm.protocol,
        "credential_available": bool(config.llm.api_key),
        "git": shutil.which("git"), "python": sys.executable,
        "git_workspace": available(["git", "rev-parse", "--is-inside-work-tree"]),
        "pytest_available": importlib.util.find_spec("pytest") is not None,
        "docker": shutil.which("docker"),
        "docker_ready": bool(shutil.which("docker")) and available(["docker", "info"]),
        "durable_runtime_enabled": config.durable_runtime.enabled,
        "runtime_store": runtime_status, "granted_scopes": trust,
        "model_validation": model_status,
        "context_window": config.llm.context_window, "max_output_tokens": config.llm.max_output_tokens,
        "enabled_mcp_servers": [server.id for server in config.mcp.servers if server.enabled],
    }
