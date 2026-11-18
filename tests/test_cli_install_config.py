from pathlib import Path
import json

from click.testing import CliRunner

from config.schema import _parse, load_config
from entry.cli import cli


def test_default_config_uses_home_not_target_repository(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".config" / "forge-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("llm:\n  model: home-model\n", encoding="utf-8")
    target = tmp_path / "target"
    (target / "config").mkdir(parents=True)
    (target / "config" / "default.yaml").write_text("llm:\n  model: wrong-model\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.chdir(target)

    assert load_config().llm.model == "home-model"
    explicit = target / "config" / "default.yaml"
    assert load_config(explicit).llm.model == "wrong-model"


def test_private_env_accepts_export_and_preserves_process_override(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text("export FORGE_TEST_API_KEY=from-file\nnot a key=ignored\n", encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text("llm:\n  api_key: ${FORGE_TEST_API_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("FORGE_ENV_FILE", str(env_file))
    monkeypatch.delenv("FORGE_TEST_API_KEY", raising=False)
    assert load_config(config_file).llm.api_key == "from-file"
    monkeypatch.setenv("FORGE_TEST_API_KEY", "from-process")
    assert load_config(config_file).llm.api_key == "from-process"


def test_config_show_selects_named_model_from_explicit_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_TEST_KEY", "private-secret")
    config_file = tmp_path / "catalog.yaml"
    config_file.write_text(
        "llm:\n  provider: openai\n  model: original\n  api_key: ${SHARED_TEST_KEY}\n"
        "  base_url: https://old.invalid/v1\n"
        "models:\n"
        "  fast:\n    model: fast-id\n    api_key: ${SHARED_TEST_KEY}\n"
        "    base_url: https://fast.invalid/v1\n"
        "  smart:\n    model: smart-id\n    api_key: ${SHARED_TEST_KEY}\n"
        "    base_url: https://smart.invalid/v1\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli, ["--config", str(config_file), "config", "show", "--model", "smart"])
    assert result.exit_code == 0
    shown = json.loads(result.output)
    assert (shown["llm"]["model"], shown["llm"]["base_url"]) == (
        "smart-id", "https://smart.invalid/v1"
    )
    assert shown["llm"]["api_key"] == "<redacted>"
    assert "private-secret" not in result.output

    backend_options = {}

    def capture_backend(options):
        backend_options.update(options)
        raise RuntimeError("backend construction captured")

    monkeypatch.setattr("entry.cli.create_backend_from_config", capture_backend)
    run = CliRunner().invoke(cli, [
        "--config", str(config_file), "run", "--repo", str(tmp_path),
        "--task", "inspect", "--model", "fast",
    ])
    assert isinstance(run.exception, RuntimeError)
    assert (backend_options["model"], backend_options["base_url"], backend_options["api_key"]) == (
        "fast-id", "https://fast.invalid/v1", "private-secret"
    )
    assert "private-secret" not in run.output


def test_run_capability_flags_override_only_current_config(tmp_path, monkeypatch):
    def stop_backend(options):
        raise RuntimeError("backend construction captured")

    monkeypatch.setattr("entry.cli.create_backend_from_config", stop_backend)
    baseline = {
        "agent": {"planning_mode": "off", "skills_enabled": False},
        "mcp": {"enabled": False, "servers": [{"id": "docs", "transport": "stdio", "command": "python"}]},
    }
    enabled = _parse(baseline)
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: enabled)
    result = CliRunner().invoke(cli, [
        "run", "--repo", str(tmp_path), "--task", "inspect", "--plan", "--skills", "--mcp",
    ])
    assert isinstance(result.exception, RuntimeError)
    assert (enabled.agent.planning_mode, enabled.agent.skills_enabled, enabled.mcp.enabled) == (
        "always", True, True
    )

    disabled = _parse({
        "agent": {"planning_mode": "always", "skills_enabled": True},
        "mcp": {"enabled": True, "servers": [{"id": "docs", "transport": "stdio", "command": "python"}]},
    })
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: disabled)
    result = CliRunner().invoke(cli, [
        "run", "--repo", str(tmp_path), "--task", "inspect", "--no-plan", "--no-skills", "--no-mcp",
    ])
    assert isinstance(result.exception, RuntimeError)
    assert (disabled.agent.planning_mode, disabled.agent.skills_enabled, disabled.mcp.enabled) == (
        "off", False, False
    )


def test_run_mcp_flag_requires_configured_server(tmp_path, monkeypatch):
    monkeypatch.setattr("entry.cli._load_cli_config", lambda ctx: _parse({}))
    result = CliRunner().invoke(cli, [
        "run", "--repo", str(tmp_path), "--task", "inspect", "--mcp",
    ])
    assert result.exit_code != 0
    assert "--mcp requires mcp.servers" in result.output
