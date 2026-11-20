import json
import os

import pytest
import yaml
from click.testing import CliRunner

from entry.cli import cli


def test_init_persists_only_keyring_reference(tmp_path, monkeypatch, record_property):
    config = tmp_path / "config.yaml"
    key = "local-synthetic-keyring-accreditation"
    credentials = {}

    def access(reference, *, password=None, delete=False):
        if delete:
            credentials.pop(reference)
        elif password is not None:
            credentials[reference] = password
        else:
            return credentials.get(reference)

    monkeypatch.setattr("config.credentials.keyring_credential", access)
    monkeypatch.setattr("entry.onboarding.create_backend_from_config",
                        lambda _: type("Provider", (), {"complete": lambda *_: None})())
    args = ["--config", str(config), "init", "--repo", str(tmp_path), "--provider", "openai",
            "--model", "fixture", "--keyring"]
    runner = CliRunner()
    result = runner.invoke(cli, args, input=f"{key}\ny\n")
    assert result.exit_code == 0, result.exception
    from config.schema import load_config

    assert load_config(config).llm.api_key == key
    assert key not in config.read_text(encoding="utf-8") + result.output
    assert not (tmp_path / "env").exists()
    assert len(credentials) == 1
    record_property("onboarding_persistence_correct", 1)


@pytest.mark.skipif(os.name != "nt", reason="Windows Credential Manager")
def test_windows_native_keyring_round_trip():
    from uuid import uuid4
    from config.credentials import PREFIX, keyring_credential

    pytest.importorskip("win32cred")
    reference = PREFIX + uuid4().hex
    keyring_credential(reference, password="synthetic-local-test-credential")
    try:
        assert keyring_credential(reference) == "synthetic-local-test-credential"
    finally:
        keyring_credential(reference, delete=True)


def test_init_checks_provider_before_writing_and_preserves_config(tmp_path, monkeypatch, record_property):
    config = tmp_path / "config.yaml"
    original = "agent:\n  max_steps: 8\nmemory:\n  enabled: false\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "private-test-credential")
    args = ["--config", str(config), "init", "--non-interactive", "--force",
            "--provider", "openai", "--model", "fixture", "--api-key-env", "FIXTURE_MODEL_KEY"]

    def fail(options):
        raise RuntimeError(options["api_key"])

    monkeypatch.setattr("entry.onboarding.create_backend_from_config", fail)
    failed = CliRunner().invoke(cli, args)
    assert failed.exit_code != 0 and "private-test-credential" not in failed.output
    assert config.read_text(encoding="utf-8") == original

    class Provider:
        def complete(self, messages, tools):
            assert not tools

    monkeypatch.setattr("entry.onboarding.create_backend_from_config", lambda _: Provider())
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.exception
    parsed = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert parsed["agent"] == {"max_steps": 8}
    assert parsed["memory"] == {"enabled": False}
    assert parsed["llm"]["api_key"] == "${FIXTURE_MODEL_KEY}"
    doctor = CliRunner().invoke(cli, ["--config", str(config), "doctor", "--json"])
    assert doctor.exit_code == 0, doctor.exception
    assert json.loads(doctor.output)["credential_available"] is True
    assert "private-test-credential" not in doctor.output
    record_property("onboarding_persistence_correct", 1)


def test_workspace_trust_grant_and_revocation(tmp_path):
    config = tmp_path / "config.yaml"
    repo = tmp_path / "repo"
    repo.mkdir()
    config.write_text("durable_runtime:\n  root: " + json.dumps(str(tmp_path / "runtime")) + "\n", encoding="utf-8")
    runner = CliRunner()
    args = ["--config", str(config), "workspace"]
    for command, extra, scopes in [
        ("status", [], []),
        ("trust", [], ["project_skills"]),
        ("revoke", ["--scope", "project_skills"], []),
    ]:
        result = runner.invoke(cli, [*args, command, "--repo", str(repo), *extra])
        assert result.exit_code == 0, result.exception
        assert json.loads(result.output)["granted_scopes"] == scopes


def test_interactive_init_records_trust_only_on_confirmation(tmp_path, monkeypatch, record_property):
    config = tmp_path / "config.yaml"
    repo = tmp_path / "repo"
    (repo / ".agents" / "skills").mkdir(parents=True)
    original = "durable_runtime:\n  root: " + json.dumps(str(tmp_path / "runtime")) + "\n"
    config.write_text(original, encoding="utf-8")
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "private-test-credential")

    class Provider:
        def complete(self, messages, tools):
            assert not tools

    monkeypatch.setattr("entry.onboarding.create_backend_from_config", lambda _: Provider())
    runner = CliRunner()
    args = ["--config", str(config), "init", "--force", "--repo", str(repo),
            "--provider", "openai", "--model", "fixture", "--api-key-env", "FIXTURE_MODEL_KEY"]
    canceled = runner.invoke(cli, args, input="y\nn\n")
    assert canceled.exit_code != 0
    assert config.read_text(encoding="utf-8") == original
    assert not (tmp_path / "runtime").exists()
    result = runner.invoke(cli, args, input="y\ny\n")
    assert result.exit_code == 0, result.exception
    assert '"granted_scopes": ["project_skills"]' in result.output
    assert "private-test-credential" not in result.output
    record_property("onboarding_persistence_correct", 1)


def test_project_skills_require_trust_when_enabled(tmp_path):
    from agent.core import Agent, AgentConfig
    from config.schema import DurableRuntimeConfig
    from durable_runtime import DurableRuntimeStore
    from durable_runtime.workspace_trust import change_trust
    from llm.base import MockBackend
    from project_memory.identity import ProjectIdentityResolver
    from tools.base import ToolRegistry

    repo = tmp_path / "repo"
    (repo / ".agents" / "skills").mkdir(parents=True)
    root = tmp_path / "runtime"
    agent = Agent(MockBackend([]), ToolRegistry(), AgentConfig(
        workspace_trust_required=True, durable_runtime_config=DurableRuntimeConfig(root=str(root)),
    ))
    assert not agent._project_skills_trusted(str(repo))
    identity = ProjectIdentityResolver.resolve(repo)
    with DurableRuntimeStore(identity, root) as store:
        change_trust(store, "project_skills", grant=True)
    assert agent._project_skills_trusted(str(repo))
