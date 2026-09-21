#!/usr/bin/env python3
"""Run three cumulative pr-test practice tasks with independent acceptance.

Dry-run is the safe default. A real LLM backend is created only when --execute is
explicitly present. The three tasks share one target working tree so B builds on A
and C builds on A+B.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT_ROOT = ROOT / "evals" / "results"
EXPECTED_BRANCH = "forge-p2-mcp-demo"
TASK_IDS = ("A", "B", "C")

TASK_A_PROMPT = r"""
Task A — Calculator Core Architecture

Work only in this repository. Refactor the calculator implementation into this package:

src/calc_core/
├── __init__.py
├── errors.py
├── operations.py
├── registry.py
└── service.py

Keep the existing top-level calculator.py as a compatibility facade.

Frozen contract:
- Preserve the existing public calculator.py functions exactly: add, subtract, multiply, divide, clamp.
- Preserve divide-by-zero behavior: divide(..., 0) raises ZeroDivisionError with the existing contract.
- Preserve clamp behavior, including lower > upper raising ValueError("lower must not exceed upper").
- src/calc_core/operations.py owns the five operation implementations.
- src/calc_core/errors.py defines CalculatorError and UnknownOperationError.
- src/calc_core/registry.py defines OperationRegistry with register(name, operation) and resolve(name).
- src/calc_core/service.py defines CalculatorService. CalculatorService() must have all five default operations registered.
- CalculatorService.execute(operation_name, *args) executes by operation name.
- An unknown operation must raise UnknownOperationError, not KeyError or a generic exception.
- Do not create a Git commit.
- Add focused tests for the new architecture.
- Run focused tests, then run the full regression suite with pytest before finishing.
- Do not reduce or weaken existing tests.

Use the existing Forge tools. Prefer file/test/git inspection tools over shell when a dedicated tool exists.
""".strip()

TASK_B_PROMPT = r"""
Task B — Batch Execution

Task A is already accepted in this same working tree. Extend CalculatorService with:

    CalculatorService.execute_batch(requests)

Frozen request/result contract:
- requests is an ordered iterable of request mappings.
- A valid request contains "operation" (non-empty str) and "args" (list or tuple).
- Preserve input order and return exactly one result per input item.
- One failing request must not abort later requests.
- Success item shape must contain: {"ok": True, "value": <operation result>}.
- Error item shape must contain: {"ok": False, "error": {"type": <exception class name>, "message": <message>}}.
- src/calc_core/errors.py adds InvalidRequestError for deterministic request-validation failures.
- Unknown operations are captured per item as error.type == "UnknownOperationError".
- Divide-by-zero is captured per item as error.type == "ZeroDivisionError".
- Invalid request items are captured per item as error.type == "InvalidRequestError".
- The existing CalculatorService.execute single-operation API must not regress.
- The top-level calculator.py public API from Task A must not regress.
- Do not create a Git commit.
- Add focused tests for batch success/error ordering and validation.
- Run focused tests, then run the full regression suite with pytest before finishing.
- Do not reduce or weaken existing tests.

Use the existing Forge tools. Prefer file/test/git inspection tools over shell when a dedicated tool exists.
""".strip()

TASK_C_PROMPT = r"""
Task C — Runtime Operation Policy

Tasks A and B are already accepted in this same working tree. Add runtime operation policy without global mutable state.

Add src/calc_core/policy.py with OperationPolicy and make CalculatorService consume it.

Frozen policy contract:
- OperationPolicy fields:
  - enabled_operations: frozenset[str] | None = None. None means all registered operations are enabled.
  - max_batch_size: int | None = None. None means no batch-size limit.
  - strict_validation: bool = False.
- CalculatorService accepts an optional policy; default policy must preserve all Task A/B behavior.
- src/calc_core/errors.py adds OperationDisabledError and BatchSizeExceededError.
- CalculatorService.execute() raises OperationDisabledError when the requested operation is disabled.
- execute_batch() captures disabled-operation failures per item using the existing structured error result.
- max_batch_size is checked before processing the batch. len(requests) == max_batch_size is allowed; exceeding it raises BatchSizeExceededError deterministically.
- Base validation from Task B remains deterministic in all modes.
- strict_validation=False keeps Task B compatibility and may ignore extra request keys.
- strict_validation=True requires each request to contain exactly the keys "operation" and "args"; extra keys become a per-item InvalidRequestError result.
- Policy is instance state owned by CalculatorService/OperationPolicy; do not use module-level mutable policy state.
- Task A top-level calculator.py API must not regress.
- Task B execute_batch API and result shape must not regress under the default policy.
- Do not create a Git commit.
- Add focused tests for default compatibility, disabled operations, max_batch_size boundary, and strict validation.
- Run focused tests, then run the full regression suite with pytest before finishing.
- Do not reduce or weaken existing tests.

Use the existing Forge tools. Prefer file/test/git inspection tools over shell when a dedicated tool exists.
""".strip()

TASK_PROMPTS = {"A": TASK_A_PROMPT, "B": TASK_B_PROMPT, "C": TASK_C_PROMPT}

A_PROBE = r'''
from calculator import add, subtract, multiply, divide, clamp
from calc_core.errors import UnknownOperationError
from calc_core.operations import add as core_add
from calc_core.registry import OperationRegistry
from calc_core.service import CalculatorService

assert add(2, 3) == 5
assert subtract(5, 8) == -3
assert multiply(-4, 3) == -12
assert divide(7, 2) == 3.5
try:
    divide(1, 0)
except ZeroDivisionError as exc:
    assert str(exc) == "division by zero"
else:
    raise AssertionError("divide-by-zero contract regressed")
assert clamp(-3, 0, 10) == 0
assert clamp(5, 0, 10) == 5
assert clamp(42, 0, 10) == 10
assert clamp(7, 3, 3) == 3
try:
    clamp(5, 10, 0)
except ValueError as exc:
    assert str(exc) == "lower must not exceed upper"
else:
    raise AssertionError("clamp invalid-bound contract regressed")

registry = OperationRegistry()
registry.register("add", core_add)
assert registry.resolve("add")(3, 4) == 7
service = CalculatorService()
assert service.execute("add", 3, 4) == 7
assert service.execute("subtract", 3, 4) == -1
assert service.execute("multiply", 3, 4) == 12
assert service.execute("divide", 9, 2) == 4.5
assert service.execute("clamp", -5, 0, 9) == 0
try:
    service.execute("does-not-exist", 1)
except UnknownOperationError:
    pass
else:
    raise AssertionError("unknown operation must raise UnknownOperationError")
'''

B_PROBE = A_PROBE + r'''
from calc_core.errors import InvalidRequestError

service = CalculatorService()
results = service.execute_batch([
    {"operation": "add", "args": [2, 3]},
    {"operation": "divide", "args": [8, 0]},
    {"operation": "multiply", "args": [4, 5]},
])
assert len(results) == 3
assert results[0].get("ok") is True and results[0].get("value") == 5
assert results[1].get("ok") is False
assert results[1].get("error", {}).get("type") == "ZeroDivisionError"
assert results[2].get("ok") is True and results[2].get("value") == 20
unknown = service.execute_batch([{"operation": "missing", "args": []}])[0]
assert unknown.get("ok") is False
assert unknown.get("error", {}).get("type") == "UnknownOperationError"
invalid = service.execute_batch([{"operation": "add"}])[0]
assert invalid.get("ok") is False
assert invalid.get("error", {}).get("type") == "InvalidRequestError"
'''

C_PROBE = B_PROBE + r'''
from calc_core.errors import BatchSizeExceededError, OperationDisabledError
from calc_core.policy import OperationPolicy

default_service = CalculatorService()
default_results = default_service.execute_batch([
    {"operation": "add", "args": [1, 2]},
    {"operation": "multiply", "args": [3, 4]},
])
assert [item.get("value") for item in default_results] == [3, 12]

restricted = CalculatorService(policy=OperationPolicy(enabled_operations=frozenset({"add"})))
assert restricted.execute("add", 2, 5) == 7
try:
    restricted.execute("subtract", 5, 2)
except OperationDisabledError:
    pass
else:
    raise AssertionError("disabled single operation must raise OperationDisabledError")
disabled_item = restricted.execute_batch([{"operation": "subtract", "args": [5, 2]}])[0]
assert disabled_item.get("ok") is False
assert disabled_item.get("error", {}).get("type") == "OperationDisabledError"

limited = CalculatorService(policy=OperationPolicy(max_batch_size=2))
assert len(limited.execute_batch([
    {"operation": "add", "args": [1, 1]},
    {"operation": "add", "args": [2, 2]},
])) == 2
try:
    limited.execute_batch([
        {"operation": "add", "args": [1, 1]},
        {"operation": "add", "args": [2, 2]},
        {"operation": "add", "args": [3, 3]},
    ])
except BatchSizeExceededError:
    pass
else:
    raise AssertionError("batch larger than max_batch_size must be rejected")

loose = CalculatorService(policy=OperationPolicy(strict_validation=False))
assert loose.execute_batch([{"operation": "add", "args": [2, 2], "extra": True}])[0].get("value") == 4
strict = CalculatorService(policy=OperationPolicy(strict_validation=True))
strict_item = strict.execute_batch([{"operation": "add", "args": [2, 2], "extra": True}])[0]
assert strict_item.get("ok") is False
assert strict_item.get("error", {}).get("type") == "InvalidRequestError"
'''

PROBES = {"A": A_PROBE, "B": B_PROBE, "C": C_PROBE}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _repo_identity(repo: Path) -> dict[str, str]:
    if not repo.exists() or not (repo / ".git").exists():
        raise ValueError(f"not a Git repository with .git metadata: {repo}")
    try:
        top = Path(_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        branch = _git(repo, "branch", "--show-current").stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise ValueError(f"cannot inspect Git repository {repo}: {exc}") from exc
    if top != repo.resolve():
        raise ValueError(f"--repo must point at the repository root: {top}")
    return {"path": str(top), "head": head, "branch": branch}


def _repo_snapshot(repo: Path) -> str:
    """Fingerprint tracked changes plus untracked file contents for resume safety."""
    digest = hashlib.sha256()
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
    diff = _git(repo, "diff", "--binary", "HEAD", "--", ".").stdout
    cached = _git(repo, "diff", "--binary", "--cached", "HEAD", "--", ".").stdout
    digest.update(status.encode("utf-8", errors="surrogateescape"))
    digest.update(diff.encode("utf-8", errors="surrogateescape"))
    digest.update(cached.encode("utf-8", errors="surrogateescape"))
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
    for rel in sorted(filter(None, untracked.split("\0"))):
        digest.update(rel.encode("utf-8", errors="surrogateescape"))
        target = repo / rel
        if target.is_file():
            digest.update(target.read_bytes())
    return digest.hexdigest()


def _is_clean(repo: Path) -> bool:
    return not _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout.strip()


def _python_env(workspace: Path) -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(workspace), str(workspace / "src")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _run_checked(command: list[str], workspace: Path, *, timeout: int) -> None:
    proc = subprocess.run(
        command,
        cwd=workspace,
        env=_python_env(workspace),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if proc.returncode != 0:
        output = (proc.stdout or "").strip()
        if len(output) > 5000:
            output = output[-5000:]
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(command)}\n{output}")


def _verify(workspace: Path, task_id: str) -> bool:
    """Independent verifier: hidden probe plus repository full regression."""
    _run_checked([sys.executable, "-c", PROBES[task_id]], workspace, timeout=30)
    _run_checked([sys.executable, "-m", "pytest", "-q"], workspace, timeout=120)
    return True


def verify_task_a(workspace: Path) -> bool:
    return _verify(workspace, "A")


def verify_task_b(workspace: Path) -> bool:
    return _verify(workspace, "B")


def verify_task_c(workspace: Path) -> bool:
    return _verify(workspace, "C")


VERIFIERS: dict[str, Callable[[Path], bool]] = {
    "A": verify_task_a,
    "B": verify_task_b,
    "C": verify_task_c,
}

VERIFIER_SUMMARIES = {
    "A": "hidden public-API + OperationRegistry/CalculatorService/domain-error probe; then full pytest",
    "B": "Task-A probe + success/failure/success batch ordering + unknown/invalid-item probe; then full pytest",
    "C": "Task-A/B probes + default/disabled/max-batch/strict-policy probe; then full pytest",
}


def _passed(record: dict[str, Any] | None) -> bool:
    return bool(
        record
        and record.get("status") == "success"
        and record.get("acceptance_status") == "passed"
        and record.get("trajectory_eligible") is True
    )


def _dependency_ids(task_id: str) -> tuple[str, ...]:
    if task_id == "A":
        return ()
    if task_id == "B":
        return ("A",)
    return ("A", "B")


def _selected_tasks(state: dict[str, Any] | None, only: str | None) -> list[str]:
    if only:
        return [only]
    records = (state or {}).get("tasks", {})
    return [task_id for task_id in TASK_IDS if not _passed(records.get(task_id))]


def _latest_artifact_dir(root: Path) -> Path | None:
    candidates = sorted(
        p for p in root.glob("pr-test-practice-batch-*")
        if p.is_dir() and (p / "batch_state.json").is_file()
    )
    return candidates[-1] if candidates else None


def _load_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"expected JSON object: {path}")
    return raw


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _summary_from_state(state: dict[str, Any]) -> dict[str, Any]:
    tasks = state.get("tasks", {})
    eligible = [
        record.get("trace_path")
        for task_id in TASK_IDS
        if (record := tasks.get(task_id)) and _passed(record) and record.get("trace_path")
    ]
    return {
        "schema_version": 1,
        "batch_id": state.get("batch_id"),
        "repo": state.get("repo"),
        "base_branch": state.get("base_branch"),
        "base_head": state.get("base_head"),
        "model": state.get("model"),
        "tasks": tasks,
        "eligible_traces_for_p2_5": eligible,
        "total_steps": sum(int((tasks.get(t) or {}).get("steps", 0)) for t in TASK_IDS),
        "total_tokens": sum(int((tasks.get(t) or {}).get("tokens", 0)) for t in TASK_IDS),
        "total_elapsed": sum(float((tasks.get(t) or {}).get("elapsed", 0.0)) for t in TASK_IDS),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _persist(artifact_dir: Path, state: dict[str, Any]) -> None:
    _write_json(artifact_dir / "batch_state.json", state)
    _write_json(artifact_dir / "batch_summary.json", _summary_from_state(state))


def _resolve_artifact_dir(args: argparse.Namespace, *, execute: bool) -> tuple[Path, dict[str, Any] | None]:
    root = Path(args.artifact_root).resolve()
    explicit = Path(args.artifact_dir).resolve() if args.artifact_dir else None
    if args.resume:
        artifact_dir = explicit or _latest_artifact_dir(root)
        if artifact_dir is None:
            raise ValueError(f"--resume found no batch_state.json under {root}")
        state = _load_json(artifact_dir / "batch_state.json")
        return artifact_dir, state
    if explicit:
        artifact_dir = explicit
    else:
        artifact_dir = root / f"pr-test-practice-batch-{_utc_timestamp()}"
    if artifact_dir.exists() and any(artifact_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty artifact directory: {artifact_dir}")
    if execute:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir, None


def _validate_repo_for_run(
    repo: Path,
    identity: dict[str, str],
    *,
    expected_branch: str,
    resume_state: dict[str, Any] | None,
) -> None:
    if identity["branch"] != expected_branch:
        raise ValueError(
            f"wrong base branch: expected {expected_branch!r}, got {identity['branch']!r}"
        )
    if resume_state is None:
        if not _is_clean(repo):
            raise ValueError("fresh batch requires a clean working tree")
        return
    if str(repo.resolve()) != resume_state.get("repo"):
        raise ValueError("resume repository path does not match batch_state.json")
    if identity["branch"] != resume_state.get("base_branch"):
        raise ValueError("resume branch does not match batch_state.json")
    if identity["head"] != resume_state.get("base_head"):
        raise ValueError("resume HEAD changed since the batch started")
    current = _repo_snapshot(repo)
    expected = resume_state.get("repo_snapshot")
    if expected and current != expected:
        raise ValueError(
            "resume working tree differs from the last recorded checkpoint; "
            "refusing to spend API tokens on an unknown state"
        )


def _print_plan(
    *,
    repo: Path,
    config_path: str | None,
    config: Any,
    artifact_dir: Path,
    state: dict[str, Any] | None,
    only: str | None,
    execute: bool,
) -> list[str]:
    selected = _selected_tasks(state, only)
    mode = "EXECUTE" if execute else "DRY RUN (API calls = 0)"
    print(f"\nForge Agent pr-test practice batch — {mode}")
    print(f"Repo       : {repo}")
    print(f"Config     : {config_path or 'config/default.yaml'}")
    print(f"Provider   : {config.llm.provider}")
    print(f"Model      : {config.llm.model}")
    print("Overrides  : planning=always, recovery=structured, skills=false, mcp=disabled, max_steps=30")
    print(f"Artifacts  : {artifact_dir}")
    print("\nTasks:")
    records = (state or {}).get("tasks", {})
    for task_id in TASK_IDS:
        if _passed(records.get(task_id)):
            disposition = "SKIP (already success + acceptance=passed + P2-5 eligible)"
        elif only and task_id != only:
            disposition = "not selected"
        else:
            disposition = "RUN" if task_id in selected else "pending"
        print(f"  {task_id}: {disposition}")
        print(f"     verifier: {VERIFIER_SUMMARIES[task_id]}")
    print("\nOrder      : " + (" -> ".join(selected) if selected else "nothing to run"))
    return selected


def _make_state(
    *,
    repo: Path,
    identity: dict[str, str],
    config: Any,
    artifact_dir: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "batch_id": artifact_dir.name,
        "repo": str(repo.resolve()),
        "base_branch": identity["branch"],
        "base_head": identity["head"],
        "repo_snapshot": _repo_snapshot(repo),
        "provider": config.llm.provider,
        "model": config.llm.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tasks": {task_id: {"status": "pending", "acceptance_status": "not_requested"} for task_id in TASK_IDS},
    }


def _build_execution_runner(repo: Path, config: Any, trace_dir: Path):
    """Execution-only composition. Dry-run never calls this function."""
    from agent.core import AgentConfig
    from agent.runner import ExecutionRunner
    from config.schema import MCPConfig
    from entry.cli import _build_run_registry
    from harness import PermissionManager
    from llm.router import create_backend_from_config

    config.agent.max_steps = 30
    config.agent.planning_mode = "always"
    config.agent.recovery_mode = "structured"
    config.agent.skills_enabled = False
    config.mcp = MCPConfig(enabled=False, servers=())

    backend = create_backend_from_config({
        "provider": config.llm.provider,
        "protocol": config.llm.protocol,
        "model": config.llm.model,
        "api_key": config.llm.api_key or None,
        "base_url": config.llm.base_url or None,
        "max_tokens": config.llm.max_tokens,
        "max_output_tokens": config.llm.max_output_tokens,
        "context_window": config.llm.context_window,
        "model_max_output_tokens": config.llm.model_max_output_tokens,
        "semantic_packet_max_tokens": config.context.semantic_packet_max_tokens,
        "context_budget_cap": config.agent.context_budget_cap,
        "context_safety_margin_tokens": config.agent.context_safety_margin_tokens,
        "strict_tool_schema": config.llm.strict_tool_schema,
    })
    registry = _build_run_registry(
        config,
        confirm_callback=None,
        runtime=None,
        default_cwd=str(repo),
        workspace=str(repo),
    )
    permission = PermissionManager(workspace=str(repo), registry=registry)
    agent_config = AgentConfig(
        max_steps=30,
        budget_tokens=config.agent.budget_tokens,
        history_max_messages=config.context.history_window * 2,
        planning_mode="always",
        recovery_mode="structured",
        recovery_max_attempts=config.agent.recovery_max_attempts,
        skills_enabled=False,
        skills_global_dir=config.agent.skills_global_dir,
        skills_max_loaded=config.agent.skills_max_loaded,
        skills_max_chars=config.agent.skills_max_chars,
        skills_reference_max_chars=config.agent.skills_reference_max_chars,
        stream=False,
        confirm_dangerous=False,
        confirm_callback=None,
    )
    runner = ExecutionRunner(
        backend=backend,
        registry=registry,
        config=agent_config,
        log_dir=str(trace_dir),
        registry_builder=_build_run_registry,
        confirm_callback=None,
        mcp_config=MCPConfig(enabled=False, servers=()),
    )
    return runner, permission


def _check_preconditions(task_id: str, repo: Path, state: dict[str, Any]) -> None:
    records = state.get("tasks", {})
    for dependency in _dependency_ids(task_id):
        if _passed(records.get(dependency)):
            continue
        try:
            VERIFIERS[dependency](repo)
        except Exception as exc:
            raise ValueError(
                f"Task {task_id} requires accepted Task {dependency}; offline precondition verifier failed: {exc}"
            ) from exc


def _trajectory_eligible(trace_path: str | None) -> tuple[bool, str]:
    if not trace_path:
        return False, "missing trace_path"
    from experience.trajectory import load_trajectory

    try:
        loaded = load_trajectory(trace_path)
    except Exception as exc:
        return False, f"trajectory_check_failed:{type(exc).__name__}:{exc}"
    normalized = loaded.normalized
    return bool(normalized.eligible), normalized.eligibility_reason


def _run_task(
    task_id: str,
    *,
    repo: Path,
    runner: Any,
    permission: Any,
    renderer: Any,
) -> tuple[Any, float, bool, str]:
    from agent.runner import AcceptanceContract, RunRequest
    from agent.task import Task

    prompt = TASK_PROMPTS[task_id]
    task = Task(
        description=prompt,
        repo_path=str(repo),
        task_id=f"pr-practice-{task_id.lower()}-{_utc_timestamp()}",
        test_cmd=f"{sys.executable} -m pytest -q",
        require_changes=True,
        require_tests=True,
        max_steps=30,
        budget_tokens=runner.config.budget_tokens,
    )
    acceptance = AcceptanceContract(
        require_changes=True,
        require_tests=True,
        verifier=VERIFIERS[task_id],
    )
    renderer.reset()
    started = time.perf_counter()
    result = runner.run(
        RunRequest(
            task=task,
            acceptance=acceptance,
            permission=permission,
            entrypoint="pr_test_practice_batch",
        ),
        on_event=renderer,
    )
    elapsed = time.perf_counter() - started
    eligible, reason = _trajectory_eligible(result.trace_path)
    return result, elapsed, eligible, reason


def _record_from_result(result: Any, elapsed: float, eligible: bool, reason: str) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "acceptance_status": result.acceptance_status,
        "acceptance_error": result.acceptance_error,
        "termination_reason": result.termination_reason,
        "resource_reason": result.resource_reason,
        "steps": result.steps_taken,
        "tokens": result.total_tokens,
        "elapsed": elapsed,
        "trace_path": result.trace_path,
        "trajectory_eligible": eligible,
        "trajectory_eligibility_reason": reason,
        "summary": result.summary,
        "error": result.error,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def _print_task_result(task_id: str, record: dict[str, Any]) -> None:
    print(f"\nTask {task_id}")
    print(f"Status     : {record.get('status')}")
    print(f"Acceptance : {record.get('acceptance_status')}")
    print(f"Steps      : {record.get('steps', 0)}")
    print(f"Tokens     : {int(record.get('tokens', 0)):,}")
    print(f"Elapsed    : {float(record.get('elapsed', 0.0)):.1f}s")
    print(f"Trace      : {record.get('trace_path')}")
    print(f"P2-5       : {'eligible' if record.get('trajectory_eligible') else 'ineligible'} ({record.get('trajectory_eligibility_reason')})")


def _print_batch_summary(state: dict[str, Any]) -> None:
    summary = _summary_from_state(state)
    print("\nBatch summary")
    for task_id in TASK_IDS:
        record = summary["tasks"].get(task_id) or {}
        status = record.get("status", "pending").upper()
        acceptance = record.get("acceptance_status", "not_requested")
        print(f"{task_id}: {status} / acceptance={acceptance}")
    print("\nEligible traces for P2-5:")
    for trace in summary["eligible_traces_for_p2_5"]:
        print(f"  {trace}")
    if not summary["eligible_traces_for_p2_5"]:
        print("  (none)")
    print(f"\nTotal steps   : {summary['total_steps']}")
    print(f"Total tokens  : {summary['total_tokens']:,}")
    print(f"Total elapsed : {summary['total_elapsed']:.1f}s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="clean pr-test working tree")
    parser.add_argument("--config", default=None, help="Forge config YAML; defaults to config/default.yaml")
    parser.add_argument("--dry-run", action="store_true", help="print checks/tasks only; provider calls are impossible")
    parser.add_argument("--execute", action="store_true", help="explicitly enable real LLM provider calls")
    parser.add_argument("--resume", action="store_true", help="resume the latest or --artifact-dir batch state")
    parser.add_argument("--only", choices=TASK_IDS, help="run only one task; dependencies are verified offline first")
    parser.add_argument("--expected-branch", default=EXPECTED_BRANCH)
    parser.add_argument("--artifact-root", default=str(DEFAULT_ARTIFACT_ROOT))
    parser.add_argument("--artifact-dir", default=None, help="explicit batch artifact directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute and args.dry_run:
        raise SystemExit("--execute and --dry-run are mutually exclusive")
    execute = bool(args.execute)

    repo = Path(args.repo).resolve()
    identity = _repo_identity(repo)

    from config.schema import load_config
    config = load_config(args.config)

    artifact_dir, state = _resolve_artifact_dir(args, execute=execute)
    _validate_repo_for_run(
        repo,
        identity,
        expected_branch=args.expected_branch,
        resume_state=state,
    )
    selected = _print_plan(
        repo=repo,
        config_path=args.config,
        config=config,
        artifact_dir=artifact_dir,
        state=state,
        only=args.only,
        execute=execute,
    )

    if not execute:
        return 0

    if state is None:
        state = _make_state(
            repo=repo,
            identity=identity,
            config=config,
            artifact_dir=artifact_dir,
        )
        _persist(artifact_dir, state)

    if not selected:
        _print_batch_summary(state)
        return 0

    from entry.event_renderer import RunEventRenderer

    trace_dir = artifact_dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    runner, permission = _build_execution_runner(repo, config, trace_dir)
    renderer = RunEventRenderer(preview_lines=5, compact=False, show_task=True, show_reasoning=False)
    exit_code = 0
    try:
        for task_id in selected:
            record = state["tasks"].get(task_id)
            if _passed(record):
                print(f"Task {task_id}: SKIP (already accepted; no API call)")
                continue
            _check_preconditions(task_id, repo, state)
            state["tasks"][task_id] = {
                "status": "running",
                "acceptance_status": "not_requested",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _persist(artifact_dir, state)

            try:
                result, elapsed, eligible, reason = _run_task(
                    task_id,
                    repo=repo,
                    runner=runner,
                    permission=permission,
                    renderer=renderer,
                )
            except BaseException as exc:
                state["tasks"][task_id] = {
                    "status": "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                    "acceptance_status": "skipped",
                    "acceptance_error": f"batch execution raised {type(exc).__name__}: {exc}",
                    "steps": 0,
                    "tokens": 0,
                    "elapsed": 0.0,
                    "trace_path": None,
                    "trajectory_eligible": False,
                    "trajectory_eligibility_reason": "batch_execution_exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
                state["repo_snapshot"] = _repo_snapshot(repo)
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                _persist(artifact_dir, state)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                print(f"\nSTOP: Task {task_id} raised {type(exc).__name__}: {exc}")
                exit_code = 1
                break

            record = _record_from_result(result, elapsed, eligible, reason)
            state["tasks"][task_id] = record
            state["repo_snapshot"] = _repo_snapshot(repo)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _persist(artifact_dir, state)
            _print_task_result(task_id, record)

            if not _passed(record):
                print(f"\nSTOP: Task {task_id} did not finish as SUCCESS + acceptance=passed + P2-5 eligible.")
                exit_code = 1
                break
    finally:
        runner.close()

    _print_batch_summary(state)
    print(f"\nArtifacts: {artifact_dir}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
