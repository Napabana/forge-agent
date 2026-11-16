from pathlib import Path

from config.schema import load_config


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
