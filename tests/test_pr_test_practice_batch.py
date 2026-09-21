from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.run_pr_test_practice_batch as batch


def _init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-b", batch.EXPECTED_BRANCH, str(path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "seed"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return path


def test_resume_selection_skips_only_fully_eligible_tasks():
    state = {
        "tasks": {
            "A": {
                "status": "success",
                "acceptance_status": "passed",
                "trajectory_eligible": True,
            },
            "B": {
                "status": "failed",
                "acceptance_status": "skipped",
                "trajectory_eligible": False,
            },
            "C": {"status": "pending", "acceptance_status": "not_requested"},
        }
    }
    assert batch._selected_tasks(state, None) == ["B", "C"]
    assert batch._selected_tasks(state, "C") == ["C"]


def test_hidden_probes_compile():
    for task_id in batch.TASK_IDS:
        compile(batch.PROBES[task_id], f"<{task_id}-probe>", "exec")


def test_default_dry_run_never_builds_provider_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        batch,
        "_build_execution_runner",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider path reached")),
    )
    code = batch.main([
        "--repo",
        str(repo),
        "--dry-run",
        "--artifact-root",
        str(tmp_path / "results"),
    ])
    assert code == 0
    assert not (tmp_path / "results").exists()


def test_resume_with_all_tasks_passed_uses_zero_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo(tmp_path / "repo")
    artifact = tmp_path / "results" / "pr-test-practice-batch-test"
    artifact.mkdir(parents=True)
    identity = batch._repo_identity(repo)
    passed = {
        "status": "success",
        "acceptance_status": "passed",
        "trajectory_eligible": True,
        "steps": 1,
        "tokens": 2,
        "elapsed": 0.1,
        "trace_path": "/tmp/trace.jsonl",
    }
    state = {
        "schema_version": 1,
        "batch_id": artifact.name,
        "repo": str(repo.resolve()),
        "base_branch": identity["branch"],
        "base_head": identity["head"],
        "repo_snapshot": batch._repo_snapshot(repo),
        "provider": "openai",
        "model": "fake",
        "tasks": {"A": dict(passed), "B": dict(passed), "C": dict(passed)},
    }
    (artifact / "batch_state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        batch,
        "_build_execution_runner",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider path reached")),
    )
    code = batch.main([
        "--repo",
        str(repo),
        "--execute",
        "--resume",
        "--artifact-dir",
        str(artifact),
    ])
    assert code == 0


def test_resume_rejects_unknown_working_tree_state(tmp_path: Path):
    repo = _init_repo(tmp_path / "repo")
    identity = batch._repo_identity(repo)
    state = {
        "repo": str(repo.resolve()),
        "base_branch": identity["branch"],
        "base_head": identity["head"],
        "repo_snapshot": batch._repo_snapshot(repo),
    }
    (repo / "manual-edit.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from the last recorded checkpoint"):
        batch._validate_repo_for_run(
            repo,
            batch._repo_identity(repo),
            expected_branch=batch.EXPECTED_BRANCH,
            resume_state=state,
        )


def test_max_steps_cli_default_and_override():
    default_args = batch.build_parser().parse_args(["--repo", ".", "--dry-run"])
    assert default_args.max_steps == batch.DEFAULT_MAX_STEPS == 60

    overridden = batch.build_parser().parse_args([
        "--repo",
        ".",
        "--dry-run",
        "--max-steps",
        "73",
    ])
    assert overridden.max_steps == 73


def test_dry_run_surfaces_custom_max_steps_without_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setattr(
        batch,
        "_build_execution_runner",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider path reached")),
    )
    code = batch.main([
        "--repo",
        str(repo),
        "--dry-run",
        "--max-steps",
        "73",
        "--artifact-root",
        str(tmp_path / "results"),
    ])
    assert code == 0
    output = capsys.readouterr().out
    assert "max_steps=73" in output


def test_max_steps_must_be_positive():
    with pytest.raises(SystemExit, match="--max-steps must be >= 1"):
        batch.main(["--repo", ".", "--dry-run", "--max-steps", "0"])
