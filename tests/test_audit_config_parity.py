import pytest
from types import SimpleNamespace

from config.schema import ContextBudgetSpec, _parse, load_config
from entry.context_common import configured_agent_options
from evals.coding_agent.__main__ import _real_runner_factory
from llm.base import MockBackend


def test_cli_and_eval_share_counter_map_test_and_cost_configuration(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("""
llm:
  provider: openai
  model: unknown-fixture
  max_output_tokens: 512
agent:
  context_budget_cap: 20000
  context_safety_margin_tokens: 500
  max_model_calls: 7
  max_total_tokens: 30000
context:
  compaction_mode: "off"
  repo_map_mode: query_aware
  repo_map_budget: 4567
tools:
  test:
    python_executable: /fixture/python
""")
    cfg = load_config(path)
    suite = SimpleNamespace(defaults={})
    factory = _real_runner_factory(cfg, MockBackend([]), suite, "off", "off", False, None, cfg.mcp)
    runner = factory(None, tmp_path, tmp_path / "trace", "fixture")
    expected = configured_agent_options(cfg)
    for name, value in expected.items():
        assert getattr(runner.config, name) == value
    assert isinstance(runner.config.budget_tokens, ContextBudgetSpec)
    assert runner.config.prepare_next_turn is None
    assert "git_commit" not in runner.registry.tool_names


@pytest.mark.parametrize("name", ["max_model_calls", "max_total_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, "bad"])
def test_invalid_cumulative_limits_are_rejected(tmp_path, name, value):
    path = tmp_path / "config.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"agent": {name: value}}))
    with pytest.raises(ValueError):
        load_config(path)


def test_eval_preserves_budget_counter_metadata_and_isolates_policy(tmp_path):
    cfg = _parse({
        "llm": {"model": "fixture-model", "max_output_tokens": 512},
        "agent": {"context_budget_cap": 20000, "context_safety_margin_tokens": 500},
    })
    factory = _real_runner_factory(
        cfg, MockBackend([]), SimpleNamespace(defaults={"budget_tokens": 12000}),
        "off", "off", False, None, cfg.mcp,
    )
    first = factory(None, tmp_path, tmp_path / "first", "first")
    second = factory(None, tmp_path, tmp_path / "second", "second")
    assert first.config.budget_tokens.context_budget_cap == 12000
    assert first.config.budget_tokens.model_name == "fixture-model"
    assert first.config.budget_tokens.request_max_output_tokens == 512
    assert first.config.prepare_next_turn is not second.config.prepare_next_turn


def test_eval_supervisor_budget_split_uses_same_total_cap(tmp_path):
    cfg = _parse({"agent": {"multi_agent_enabled": False}})
    suite = SimpleNamespace(defaults={})
    baseline = _real_runner_factory(
        cfg, MockBackend([]), suite, "off", "off", False, None, cfg.mcp,
        "baseline_react", 60_000,
    )(None, tmp_path, tmp_path / "baseline", "baseline")
    supervisor = _real_runner_factory(
        cfg, MockBackend([]), suite, "off", "off", False, None, cfg.mcp,
        "supervisor_workers", 60_000,
    )(None, tmp_path, tmp_path / "supervisor", "supervisor")
    assert baseline.config.max_total_tokens == 60_000
    assert supervisor.config.max_total_tokens == baseline.config.max_total_tokens
    assert supervisor.config.total_worker_tokens == 20_000
    assert supervisor.config.multi_agent_enabled
    assert supervisor.worker_backend_factory is not None
