from pathlib import Path
from types import SimpleNamespace
import os
import stat

from click.testing import CliRunner

from config.schema import load_config
from entry.cli import cli
from skills.catalog import SkillCatalog


def _skill(root: Path, name: str = "ponytail") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use for small coding tasks.\n---\nKeep changes small.\n",
        encoding="utf-8",
    )
    return folder


def test_skill_add_local_and_list_preserves_source(tmp_path):
    source = _skill(tmp_path / "source")
    (source / "references").mkdir()
    (source / "references" / "notes.md").write_text("helpful notes", encoding="utf-8")
    root = tmp_path / "global-skills"
    config = tmp_path / "config.yaml"
    config.write_text(f"agent:\n  skills_global_dir: {root.as_posix()}\n", encoding="utf-8")
    runner = CliRunner()

    added = runner.invoke(cli, ["--config", str(config), "skill", "add", str(source)])
    listed = runner.invoke(cli, ["--config", str(config), "skill", "list"])
    duplicate = runner.invoke(cli, ["--config", str(config), "skill", "add", str(source)])

    assert added.exit_code == 0, added.output
    assert "ponytail\tglobal" in listed.output
    assert (root / "ponytail" / "references" / "notes.md").read_text() == "helpful notes"
    assert duplicate.exit_code != 0
    assert len(SkillCatalog.discover(tmp_path, global_root=root).skills) == 1


def test_managed_skill_update_preserves_source_and_removes_installation(tmp_path):
    from entry.resource_commands import install_skill, managed_skill, update_managed_skill

    source = _skill(tmp_path / "source")
    root = tmp_path / "skills"
    installed = install_skill(str(source), root)
    old_digest = managed_skill(root, "ponytail")[1]["content_digest"]
    (source / "new.md").write_text("updated", encoding="utf-8")
    assert update_managed_skill(root, "ponytail")["content_digest"] != old_digest
    assert (installed / "new.md").read_text(encoding="utf-8") == "updated"
    update_managed_skill(root, "ponytail", remove=True)
    assert not installed.exists()
    assert source.is_dir()


def test_skill_update_recovers_fatal_interruption_and_serializes_processes(tmp_path, record_property):
    import json
    import subprocess
    import sys

    from entry.resource_commands import install_skill, managed_skill

    source = _skill(tmp_path / "source")
    root = tmp_path / "skills"
    installed = install_skill(str(source), root)
    previous = managed_skill(root, "ponytail")[1]["content_digest"]
    (source / "extra.md").write_text("updated", encoding="utf-8")
    cut = """
import os, sys
from pathlib import Path
from entry import resource_commands as resources
root = Path(sys.argv[1])
original = resources._refresh_skill_index
def crash(folder):
    if folder == root:
        os._exit(38)
    original(folder)
resources._refresh_skill_index = crash
resources.update_managed_skill(root, "ponytail")
"""
    assert subprocess.run([sys.executable, "-c", cut, str(root)], timeout=20).returncode == 38
    assert managed_skill(root, "ponytail")[1]["content_digest"] == previous
    assert not (installed / "extra.md").exists()
    update = (
        "import sys; from pathlib import Path; from entry.resource_commands import update_managed_skill;"
        "update_managed_skill(Path(sys.argv[1]), 'ponytail')"
    )
    peers = [subprocess.Popen([sys.executable, "-c", update, str(root)]) for _ in range(2)]
    try:
        assert [process.wait(timeout=20) for process in peers] == [0, 0]
    finally:
        for process in peers:
            if process.poll() is None:
                process.kill()
                process.wait()
    index = json.loads((root / "skills.lock.json").read_text(encoding="utf-8"))
    assert index["skills"] == [managed_skill(root, "ponytail")[1]]
    assert (installed / "extra.md").exists()
    record_property("skill_recovery_correct", 1)


def test_mcp_lifecycle_preserves_unrelated_values(tmp_path):
    import yaml
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  model: original\nmcp:\n  enabled: false\n  servers:\n"
                      "    - id: docs\n      transport: stdio\n      command: python3\n", encoding="utf-8")
    runner = CliRunner()
    args = ["--config", str(config), "mcp"]
    for action in ("inspect", "disable", "enable", "remove"):
        result = runner.invoke(cli, [*args, action, "docs"])
        assert result.exit_code == 0, result.exception
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed == {"llm": {"model": "original"}, "mcp": {"enabled": False, "servers": []}}


def test_skill_add_github_tree_uses_only_requested_folder(tmp_path, monkeypatch):
    from entry import resource_commands

    def fake_clone(argv, **kwargs):
        if argv[1] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="a" * 40)
        checkout = Path(argv[-1])
        _skill(checkout / "skills")
        _skill(checkout / "skills", "other")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(resource_commands.subprocess, "run", fake_clone)
    destination = tmp_path / "global"
    installed = resource_commands.install_skill(
        "https://github.com/DietrichGebert/ponytail/tree/main/skills/ponytail",
        destination,
    )

    assert installed == destination / "ponytail"
    assert (installed / "SKILL.md").is_file()
    assert not (destination / "other").exists()


def test_mcp_add_preserves_model_comments_and_existing_servers(tmp_path):
    config = tmp_path / "config.yaml"
    original = (
        "# keep this model comment\n"
        "llm:\n  provider: openai\n  model: existing-model\n"
        "mcp:\n  enabled: false\n  servers:\n"
        "    - id: original\n      transport: stdio\n      command: python3\n"
        "agent:\n  skills_enabled: false\n"
    )
    config.write_text(original, encoding="utf-8")
    config.chmod(0o600)
    runner = CliRunner()

    added = runner.invoke(cli, [
        "--config", str(config), "mcp", "add", "search",
        "--url", "https://example.com/mcp",
    ])
    listed = runner.invoke(cli, ["--config", str(config), "mcp", "list"])
    duplicate = runner.invoke(cli, [
        "--config", str(config), "mcp", "add", "search",
        "--url", "https://example.com/mcp",
    ])

    assert added.exit_code == 0, added.output
    assert config.read_text(encoding="utf-8").startswith(original.split("mcp:\n")[0])
    assert "# keep this model comment" in config.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert [server.id for server in load_config(config).mcp.servers] == ["original", "search"]
    assert "original\tstdio" in listed.output and "search\tstreamable_http" in listed.output
    assert duplicate.exit_code != 0


def test_mcp_add_stdio_env_reference_and_inline_empty_list(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("mcp:\n  enabled: false\n  servers: [] # keep\n", encoding="utf-8")
    monkeypatch.setenv("EXAMPLE_TOKEN", "private-value")

    result = CliRunner().invoke(cli, [
        "--config", str(config), "mcp", "add", "docs",
        "--command", "python3", "--arg", "/tmp/server.py", "--env", "EXAMPLE_TOKEN",
    ])

    assert result.exit_code == 0, result.output
    assert "private-value" not in config.read_text(encoding="utf-8")
    assert "# keep" in config.read_text(encoding="utf-8")
    server = load_config(config).mcp.servers[0]
    assert server.command == "python3"
    assert server.args == ("/tmp/server.py",)
    assert server.env == {"EXAMPLE_TOKEN": "private-value"}
    shown = CliRunner().invoke(cli, ["--config", str(config), "config", "show"])
    assert shown.exit_code == 0
    assert "private-value" not in shown.output


def test_mcp_add_rejects_invalid_server_without_rewriting_config(tmp_path):
    config = tmp_path / "config.yaml"
    original = "llm:\n  model: unchanged\n"
    config.write_text(original, encoding="utf-8")

    result = CliRunner().invoke(cli, [
        "--config", str(config), "mcp", "add", "bad",
        "--url", "not-a-url",
    ])

    assert result.exit_code != 0
    assert config.read_text(encoding="utf-8") == original


def test_mcp_add_creates_server_block_when_absent(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  model: original\n", encoding="utf-8")

    result = CliRunner().invoke(cli, [
        "--config", str(config), "mcp", "add", "docs",
        "--command", "python3", "--arg", "/tmp/server.py",
    ])

    assert result.exit_code == 0, result.output
    parsed = load_config(config)
    assert parsed.llm.model == "original"
    assert parsed.mcp.servers[0].id == "docs"


def test_skill_add_local_uses_project_catalog(tmp_path):
    source = _skill(tmp_path / "source", "review")
    repo = tmp_path / "repo"
    repo.mkdir()

    result = CliRunner().invoke(cli, [
        "skill", "add", str(source), "--local", "--repo", str(repo),
    ])

    assert result.exit_code == 0, result.output
    skill = SkillCatalog.discover(repo).get("review")
    assert skill is not None and skill.source == "project"


def test_config_path_and_models_are_compact_and_redacted(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "llm:\n  provider: openai\n  model: base\n  api_key: private-secret\n"
        "models:\n  fast:\n    model: fast-id\n    api_key: private-secret\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    path = runner.invoke(cli, ["--config", str(config), "config", "path"])
    models = runner.invoke(cli, ["--config", str(config), "config", "models"])

    assert path.exit_code == 0 and path.output.strip() == str(config)
    assert models.exit_code == 0 and "fast\topenai\tfast-id" in models.output
    assert "private-secret" not in models.output
