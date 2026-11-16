"""B1 Context Policy 离线 benchmark：固定 History replay，不运行真实 Coding Agent。"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from agent.core import PrepareNextTurnContext
from agent.event_log import EventLog
from agent.task import Action, ActionType, Observation, ObservationStatus, Task, ToolCall
from config.schema import load_config
from context.compaction import TraceableCompaction
from context.history import ConversationHistory
from context.history_evidence import (
    iter_interactions,
    parse_action_message,
    parse_observation_message,
)
from context.repo_map import RepoMap
from context.structured_compaction import (
    LLMSemanticSummarizer,
    SemanticContextFields,
    SemanticSummaryResult,
)
from context.token_budget import (
    TokenBudget,
    estimate_messages_tokens,
    recent_history_units,
)
from context.tool_pruning import DeterministicToolPruner
from llm.base import LLMMessage
from llm.router import create_backend
from llm.usage import TokenUsage


_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = Path(__file__).parent / "fixtures" / "context_policy_benchmark.json"
_VARIANTS = ("budget_trim_only", "deterministic_pruning", "hybrid_compaction")


class FixtureSemanticSummarizer:
    """Replay 模式的确定性 semantic summarizer；不调用网络。"""

    def __init__(self, raw_fields: dict[str, Any]) -> None:
        self.fields = SemanticContextFields(
            hard_constraints=tuple(raw_fields.get("hard_constraints", ())),
            decisions=tuple(raw_fields.get("decisions", ())),
            completed=tuple(raw_fields.get("completed", ())),
            in_progress=tuple(raw_fields.get("in_progress", ())),
            blocked=tuple(raw_fields.get("blocked", ())),
            next_actions=tuple(raw_fields.get("next_actions", ())),
        )
        self.call_count = 0

    def summarize(self, **_: Any) -> SemanticSummaryResult:
        self.call_count += 1
        return SemanticSummaryResult(
            fields=self.fields,
            usage=TokenUsage(),
            packet_truncated=False,
            error=None,
        )


def load_manifest(path: str | Path = _MANIFEST) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported context policy benchmark schema")
    if not isinstance(raw.get("cases"), list) or not raw["cases"]:
        raise ValueError("benchmark manifest must contain cases")
    return raw


def run_benchmark(
    repo: str | Path = _ROOT,
    manifest: str | Path = _MANIFEST,
    output: str | Path = "evals/results/context_policy_benchmark",
    *,
    semantic_mode: str = "fixture",
    config_path: str | Path | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """运行固定 History replay，并写 raw.jsonl/report/metadata。"""
    repo_root = Path(repo).resolve()
    manifest_path = Path(manifest).resolve()
    output_dir = Path(output).resolve()
    data = load_manifest(manifest_path)
    if semantic_mode not in {"fixture", "live"}:
        raise ValueError("semantic_mode must be fixture or live")
    if not allow_dirty and _git_dirty(repo_root):
        raise RuntimeError("benchmark requires a clean worktree; pass allow_dirty=True only for local debugging")

    backend = None
    semantic_metadata: dict[str, Any] = {"mode": semantic_mode}
    if semantic_mode == "live":
        cfg = load_config(config_path)
        backend = create_backend(
            provider=cfg.llm.provider,
            protocol=cfg.llm.protocol,
            model=cfg.llm.model,
            api_key=cfg.llm.api_key or None,
            base_url=cfg.llm.base_url or None,
            max_tokens=cfg.llm.max_tokens,
        )
        semantic_metadata.update({
            "provider": cfg.llm.provider,
            "protocol": cfg.llm.protocol,
            "model": cfg.llm.model,
            "max_tokens": cfg.llm.max_tokens,
        })

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="forge-context-b1-") as tmp:
        workspace = Path(tmp)
        for case in data["cases"]:
            for variant in case.get("applicable_variants", _VARIANTS):
                rows.append(_evaluate_case_variant(
                    case=case,
                    defaults=data["defaults"],
                    variant=variant,
                    workspace=workspace / case["id"] / variant,
                    semantic_mode=semantic_mode,
                    live_backend=backend,
                ))

    metadata = {
        "schema_version": 1,
        "runner_revision": _git_revision(repo_root),
        "fixture_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "semantic": semantic_metadata,
        "variants": list(_VARIANTS),
        "case_count": len(data["cases"]),
    }
    report = _write_results(output_dir, metadata, rows)
    return report


def _evaluate_case_variant(
    *,
    case: dict[str, Any],
    defaults: dict[str, Any],
    variant: str,
    workspace: Path,
    semantic_mode: str,
    live_backend,
) -> dict[str, Any]:
    if variant not in _VARIANTS:
        raise ValueError(f"unknown context variant: {variant}")
    workspace.mkdir(parents=True, exist_ok=True)
    repo = _init_fixture_repo(workspace / "repo")
    cfg = {**defaults, **case.get("overrides", {})}
    task = Task(
        case["current_goal"],
        str(repo),
        task_id=f"b1-{case['id']}-{variant}",
        budget_tokens=int(cfg["budget_tokens"]),
    )
    event_log = EventLog.create(task, log_dir=str(workspace / "logs"))
    event_log.log_task_start(task)
    history = ConversationHistory(max_messages=None)
    history.add(LLMMessage(role="user", content=case["current_goal"]))
    next_step = _append_specs(history, event_log, case.get("history_spec", ()), 1)
    expanded_history_sha256 = _stable_history_hash(history.to_list())

    token_budget = TokenBudget(total=int(cfg["budget_tokens"]))
    system_text = str(cfg.get("system_text", ""))
    repo_map_text = str(cfg.get("repo_map_text", ""))
    threshold = float(cfg["threshold"])
    keep_recent_tokens = int(cfg["keep_recent_tokens"])
    canonical_unchanged = True
    policy_calls = 0
    pruning_event_ids: tuple[str, ...] = ()
    policy_view = history.to_list()
    policy = None
    summarizer = None

    if variant == "hybrid_compaction":
        if semantic_mode == "fixture":
            summarizer = FixtureSemanticSummarizer(case.get("expected_semantic_fields", {}))
        else:
            if live_backend is None:
                raise RuntimeError("live semantic mode requires a backend")
            summarizer = LLMSemanticSummarizer(live_backend)
        policy = TraceableCompaction(
            threshold=threshold,
            target_ratio=float(cfg["target_ratio"]),
            keep_recent_tokens=keep_recent_tokens,
            max_summary_chars=int(cfg["max_summary_chars"]),
            semantic_summarizer=summarizer,
        )
        restore_id = case.get("restore_checkpoint_id")
        if restore_id:
            policy.restore_checkpoint_lineage(str(restore_id))

        def invoke(step: int) -> list[LLMMessage]:
            nonlocal canonical_unchanged, policy_calls
            before = _canonical_hash(history.to_list())
            result = policy(_context(
                task=task,
                step=step,
                history=history,
                token_budget=token_budget,
                repo=repo,
                event_log=event_log,
                system_text=system_text,
                repo_map_text=repo_map_text,
            ))
            canonical_unchanged = canonical_unchanged and before == _canonical_hash(history.to_list())
            policy_calls += 1
            return list(result.history_override) if result and result.history_override is not None else history.to_list()

        policy_view = invoke(1 if case.get("restore_checkpoint_id") else max(2, next_step))
        for phase_index, phase in enumerate(case.get("phases", ()), start=1):
            _apply_repo_write(repo, phase.get("repo_write"))
            next_step = _append_specs(history, event_log, phase.get("append", ()), next_step)
            policy_view = invoke(max(2, next_step + phase_index))
    else:
        for phase in case.get("phases", ()):
            _apply_repo_write(repo, phase.get("repo_write"))
            next_step = _append_specs(history, event_log, phase.get("append", ()), next_step)
        before = _canonical_hash(history.to_list())
        if variant == "deterministic_pruning":
            raw = history.to_dicts()
            pressure = token_budget.request_pressure(
                system_text=system_text,
                repo_map_text=repo_map_text,
                history=raw,
                tools=(),
            )
            if pressure.ratio >= threshold:
                recent = recent_history_units(raw, keep_recent_tokens)
                if recent:
                    protected_from = recent[0].indices[0]
                    pruning = DeterministicToolPruner().prune(
                        history.to_list(),
                        protected_from_index=protected_from,
                    )
                    policy_view = list(pruning.messages)
                    pruning_event_ids = pruning.pruned_event_ids
        canonical_unchanged = before == _canonical_hash(history.to_list())
        policy_calls = 1

    raw_history = history.to_dicts()
    raw_history_tokens = estimate_messages_tokens(raw_history)
    raw_pressure = token_budget.request_pressure(
        system_text=system_text,
        repo_map_text=repo_map_text,
        history=raw_history,
        tools=(),
    )
    policy_dicts = _message_dicts(policy_view)
    policy_tokens = estimate_messages_tokens(policy_dicts)
    history_limit = token_budget.history_limit_for_request(system_text, ())
    final_dicts = token_budget.trim_history(policy_dicts, history_limit)
    final_tokens = estimate_messages_tokens(final_dicts)
    final_pressure = token_budget.request_pressure(
        system_text=system_text,
        repo_map_text=repo_map_text,
        history=final_dicts,
        tools=(),
    )
    final_messages = _dicts_to_messages(final_dicts)
    final_text = "\n".join(message.content for message in final_messages)
    canonical_messages = history.to_list()
    invariants = case.get("expected_invariants", {})
    orphan_actions, orphan_observations = _count_orphans(final_messages)
    constraint_recall = _marker_group_recall(final_text, invariants.get("constraint_markers", ()))
    recent_raw_recall = _marker_recall(final_text, invariants.get("recent_markers", ()))
    old_marker_violations = _present_markers(final_text, invariants.get("old_markers_absent_after_pruning", ()))
    stale_failure_violations = _present_markers(final_text, invariants.get("stale_failure_markers", ()))
    latest_test_status = _latest_test_status(final_messages, final_text)
    working = _working_set_metrics(
        canonical_messages,
        final_text,
        invariants.get("expected_read_paths", ()),
        invariants.get("expected_modified_paths", ()),
    )

    checkpoints = list(policy.checkpoints) if policy is not None else []
    entries = list(policy.entries) if policy is not None else []
    replayed = event_log.replay()
    event_ids = {event.event_id for event in replayed}
    checkpoint_refs = {
        event_id
        for checkpoint in checkpoints
        for event_id in (*checkpoint.source_event_ids, *checkpoint.pruned_event_ids)
    }
    if pruning_event_ids:
        checkpoint_refs.update(pruning_event_ids)
    raw_event_traceable = checkpoint_refs <= event_ids
    source_event_coverage = (
        sum(event_id in event_ids for event_id in checkpoint_refs) / len(checkpoint_refs)
        if checkpoint_refs else 1.0
    )
    checkpoint_atomic = _checkpoint_atomic(checkpoints, entries)
    lineage_correct = _lineage_correct(checkpoints, case.get("restore_checkpoint_id"))
    revisions = [checkpoint.repo_revision for checkpoint in checkpoints]
    repo_revision_changed = len(revisions) >= 2 and len(set(revisions)) > 1
    summary_usage = _sum_summary_usage(checkpoints)
    summary_call_count = int(getattr(summarizer, "call_count", 0)) if summarizer is not None else 0
    semantic_duration_ms = sum(
        checkpoint.semantic_duration_ms or 0.0 for checkpoint in checkpoints
    )
    current_user_marker = invariants.get("current_user_marker")
    current_user_count = final_text.count(str(current_user_marker)) if current_user_marker else None

    failures = _contract_failures(
        variant=variant,
        invariants=invariants,
        canonical_unchanged=canonical_unchanged,
        orphan_actions=orphan_actions,
        orphan_observations=orphan_observations,
        constraint_recall=constraint_recall,
        recent_raw_recall=recent_raw_recall,
        old_marker_violations=old_marker_violations,
        stale_failure_violations=stale_failure_violations,
        latest_test_status=latest_test_status,
        working=working,
        checkpoints=checkpoints,
        lineage_correct=lineage_correct,
        repo_revision_changed=repo_revision_changed,
        summary_call_count=summary_call_count,
        policy_calls=policy_calls,
        current_user_count=current_user_count,
    )

    event_log.close()
    return {
        "case_id": case["id"],
        "variant": variant,
        "passed": not failures,
        "failures": failures,
        "expanded_history_sha256": expanded_history_sha256,
        "canonical_messages": len(canonical_messages),
        "raw_history_tokens": raw_history_tokens,
        "policy_tokens": policy_tokens,
        "final_tokens": final_tokens,
        "history_limit_tokens": history_limit,
        "compaction_ratio": (1.0 - final_tokens / raw_history_tokens) if raw_history_tokens else 0.0,
        "raw_projected_input_tokens": raw_pressure.projected_input,
        "final_projected_input_tokens": final_pressure.projected_input,
        "raw_pressure_ratio": raw_pressure.ratio,
        "final_pressure_ratio": final_pressure.ratio,
        "hard_constraint_recall": constraint_recall,
        "recent_raw_recall": recent_raw_recall,
        "orphan_actions": orphan_actions,
        "orphan_observations": orphan_observations,
        "latest_test_status": latest_test_status,
        "stale_failure_violations": stale_failure_violations,
        "old_marker_violations": old_marker_violations,
        **working,
        "canonical_unchanged": canonical_unchanged,
        "checkpoint_count": len(checkpoints),
        "checkpoint_atomic": checkpoint_atomic,
        "lineage_correct": lineage_correct,
        "repo_revision_changed": repo_revision_changed,
        "source_event_coverage": source_event_coverage,
        "raw_event_traceable": raw_event_traceable,
        "policy_calls": policy_calls,
        "summary_call_count": summary_call_count,
        "summary_input_tokens": summary_usage["input_tokens"],
        "summary_cached_tokens": summary_usage["cached_tokens"],
        "summary_output_tokens": summary_usage["output_tokens"],
        "summary_total_tokens": summary_usage["total_tokens"],
        "semantic_duration_ms": semantic_duration_ms,
        "semantic_packet_truncated": any(checkpoint.semantic_packet_truncated for checkpoint in checkpoints),
        "semantic_error_count": sum(bool(checkpoint.semantic_error) for checkpoint in checkpoints),
        "active_view_reused": bool(
            variant == "hybrid_compaction"
            and policy_calls > summary_call_count
            and summary_call_count > 0
        ),
        "current_user_count": current_user_count,
        "checkpoint_repo_revisions": revisions,
    }


def _context(
    *,
    task: Task,
    step: int,
    history: ConversationHistory,
    token_budget: TokenBudget,
    repo: Path,
    event_log: EventLog,
    system_text: str,
    repo_map_text: str,
) -> PrepareNextTurnContext:
    return PrepareNextTurnContext(
        task=task,
        step=step,
        history=history,
        repo_map=RepoMap(repo),
        token_budget=token_budget,
        cancel_event=None,
        event_log=event_log,
        system_content=system_text,
        repo_map_content=repo_map_text,
        tool_schemas=(),
    )


def _append_specs(
    history: ConversationHistory,
    event_log: EventLog,
    specs: Iterable[dict[str, Any]],
    next_step: int,
) -> int:
    step = next_step
    for spec in specs:
        kind = spec["kind"]
        if kind == "user":
            history.add(LLMMessage(role="user", content=str(spec["content"])))
        elif kind == "user_repeat":
            for index in range(1, int(spec["count"]) + 1):
                history.add(LLMMessage(
                    role="user",
                    content=str(spec["template"]).format(i=index),
                ))
        elif kind == "tool":
            _append_tool(history, event_log, spec, step, index=1)
            step += 1
        elif kind == "tool_repeat":
            for index in range(1, int(spec["count"]) + 1):
                _append_tool(history, event_log, spec, step, index=index)
                step += 1
        else:
            raise ValueError(f"unknown history_spec kind: {kind}")
    return step


def _append_tool(
    history: ConversationHistory,
    event_log: EventLog,
    spec: dict[str, Any],
    step: int,
    *,
    index: int,
) -> None:
    tool = str(spec["tool"])
    params = _format_value(spec.get("params", {}), index)
    action = Action(
        action_type=ActionType.TOOL_CALL,
        thought=f"fixture step {step}",
        tool_call=ToolCall(name=tool, params=params),
    )
    action_ref = event_log.log_action(step, action)
    history.add(LLMMessage(
        role="assistant",
        content=(
            f"Thought: {action.thought}\n"
            f"Action: {tool}\n"
            f"Params: {json.dumps(params, ensure_ascii=False)}"
        ),
        event_ref=action_ref,
    ))

    raw_status = str(spec.get("status", "SUCCESS")).upper()
    status = ObservationStatus(raw_status.lower())
    output = _expand_text(spec.get("output", ""), index)
    error = _expand_text(spec.get("error", ""), index) or None
    observation = Observation(
        status=status,
        output=output,
        tool_name=tool,
        error=error,
    )
    observation_ref = event_log.log_observation(step, observation)
    history.add(LLMMessage(
        role="user",
        content=_format_observation(observation),
        event_ref=observation_ref,
    ))


def _format_observation(observation: Observation) -> str:
    status = "SUCCESS" if observation.is_success() else "ERROR"
    lines = [f"[Tool: {observation.tool_name} | {status}]"]
    if observation.output:
        lines.append(observation.output)
    if observation.error and not observation.is_success():
        lines.append(f"Error: {observation.error}")
    return "\n".join(lines)


def _expand_text(value: Any, index: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.format(i=index)
    if isinstance(value, dict):
        template = str(value.get("template", ""))
        repeat = int(value.get("repeat", 1))
        return "\n".join(template.format(i=index, j=j) for j in range(1, repeat + 1))
    return str(value)


def _format_value(value: Any, index: int) -> Any:
    if isinstance(value, str):
        return value.format(i=index)
    if isinstance(value, list):
        return [_format_value(item, index) for item in value]
    if isinstance(value, dict):
        return {key: _format_value(item, index) for key, item in value.items()}
    return value


def _apply_repo_write(repo: Path, spec: dict[str, Any] | None) -> None:
    if not spec:
        return
    path = (repo / str(spec["path"])).resolve()
    if repo.resolve() not in path.parents:
        raise ValueError("repo_write escapes fixture repository")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(spec.get("content", "")), encoding="utf-8")


def _init_fixture_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    commands = [
        ("init", "-q"),
        ("config", "user.email", "forge-benchmark@example.invalid"),
        ("config", "user.name", "Forge Benchmark"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "fixture seed"),
    ]
    for args in commands:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            break
    return repo


def _count_orphans(messages: list[LLMMessage]) -> tuple[int, int]:
    orphan_actions = 0
    orphan_observations = 0
    for index, message in enumerate(messages):
        action = parse_action_message(message)
        if action is not None:
            observation = parse_observation_message(messages[index + 1]) if index + 1 < len(messages) else None
            if observation is None or observation.tool_name != action.tool_name:
                orphan_actions += 1
        observation = parse_observation_message(message)
        if observation is not None:
            action_before = parse_action_message(messages[index - 1]) if index > 0 else None
            if action_before is None or action_before.tool_name != observation.tool_name:
                orphan_observations += 1
    return orphan_actions, orphan_observations


def _latest_test_status(messages: list[LLMMessage], text: str) -> str:
    latest = None
    for interaction in iter_interactions(messages):
        if interaction.action.tool_name in {"test", "pytest"}:
            latest = "PASS" if interaction.observation.status == "SUCCESS" else "FAIL"
    if latest is not None:
        return latest
    for status in ("PASS", "FAIL", "UNKNOWN"):
        if f"## Verification State\n- {status}" in text:
            return status
    return "UNKNOWN"


def _working_set_metrics(
    canonical: list[LLMMessage],
    final_text: str,
    expected_read: Iterable[str],
    expected_modified: Iterable[str],
) -> dict[str, float]:
    known_paths: set[str] = set()
    for interaction in iter_interactions(canonical):
        params = interaction.action.params
        path = params.get("path")
        if isinstance(path, str) and path:
            known_paths.add(path)
        paths = params.get("paths")
        if isinstance(paths, list):
            known_paths.update(str(item) for item in paths)
    expected = set(expected_read) | set(expected_modified)
    visible = {path for path in known_paths if path in final_text}
    true_positive = len(visible & expected)
    recall = true_positive / len(expected) if expected else 1.0
    precision = true_positive / len(visible) if visible else (1.0 if not expected else 0.0)
    return {
        "working_set_recall": recall,
        "working_set_precision": precision,
    }


def _marker_group_recall(text: str, groups: Iterable[Iterable[str]]) -> float:
    groups = list(groups)
    if not groups:
        return 1.0
    lowered = text.lower()
    hit = 0
    for group in groups:
        if all(str(marker).lower() in lowered for marker in group):
            hit += 1
    return hit / len(groups)


def _marker_recall(text: str, markers: Iterable[str]) -> float:
    markers = list(markers)
    if not markers:
        return 1.0
    return sum(str(marker) in text for marker in markers) / len(markers)


def _present_markers(text: str, markers: Iterable[str]) -> int:
    return sum(str(marker) in text for marker in markers)


def _checkpoint_atomic(checkpoints, entries) -> bool:
    checkpoint_by_id = {item.checkpoint_id: item for item in checkpoints}
    if len(checkpoint_by_id) != len(checkpoints):
        return False
    for entry in entries:
        checkpoint = checkpoint_by_id.get(entry.checkpoint_id)
        if checkpoint is None or checkpoint.summary_hash != entry.summary_hash:
            return False
    return True


def _lineage_correct(checkpoints, restored_id: str | None) -> bool:
    previous = restored_id
    for checkpoint in checkpoints:
        if checkpoint.previous_checkpoint_id != previous:
            return False
        previous = checkpoint.checkpoint_id
    return True


def _sum_summary_usage(checkpoints) -> dict[str, int]:
    keys = ("input_tokens", "cached_tokens", "output_tokens", "total_tokens")
    totals = {key: 0 for key in keys}
    for checkpoint in checkpoints:
        usage = checkpoint.summary_usage or {}
        for key in keys:
            totals[key] += int(usage.get(key, 0))
    return totals


def _contract_failures(
    *,
    variant: str,
    invariants: dict[str, Any],
    canonical_unchanged: bool,
    orphan_actions: int,
    orphan_observations: int,
    constraint_recall: float,
    recent_raw_recall: float,
    old_marker_violations: int,
    stale_failure_violations: int,
    latest_test_status: str,
    working: dict[str, float],
    checkpoints,
    lineage_correct: bool,
    repo_revision_changed: bool,
    summary_call_count: int,
    policy_calls: int,
    current_user_count: int | None,
) -> list[str]:
    failures: list[str] = []
    if not canonical_unchanged:
        failures.append("canonical_history_mutated")
    if orphan_actions or orphan_observations:
        failures.append("orphan_action_observation")
    if recent_raw_recall < 1.0:
        failures.append("recent_raw_marker_lost")

    if variant in {"deterministic_pruning", "hybrid_compaction"} and old_marker_violations:
        failures.append("pruned_old_marker_visible")

    if variant == "hybrid_compaction":
        if invariants.get("constraint_markers") and constraint_recall < 1.0:
            failures.append("hard_constraint_lost")
        expected_verification = invariants.get("verification_status")
        if expected_verification and latest_test_status != expected_verification:
            failures.append("latest_verification_incorrect")
        if stale_failure_violations:
            failures.append("superseded_failure_still_visible")
        if invariants.get("expected_read_paths") or invariants.get("expected_modified_paths"):
            if working["working_set_recall"] < 1.0:
                failures.append("working_set_incomplete")
        expected_calls = invariants.get("expected_summary_calls")
        if expected_calls is not None and summary_call_count != int(expected_calls):
            failures.append("summary_call_count_mismatch")
        expected_policy_calls = invariants.get("expected_policy_calls")
        if expected_policy_calls is not None and policy_calls != int(expected_policy_calls):
            failures.append("policy_call_count_mismatch")
        expected_previous = invariants.get("expected_previous_checkpoint_id")
        if expected_previous is not None:
            if not checkpoints or checkpoints[0].previous_checkpoint_id != expected_previous:
                failures.append("resume_lineage_missing")
        if invariants.get("require_lineage") and not lineage_correct:
            failures.append("checkpoint_lineage_broken")
        if invariants.get("require_repo_revision_change") and not repo_revision_changed:
            failures.append("repo_revision_did_not_change")
        if invariants.get("require_active_reuse") and not (
            policy_calls > summary_call_count and summary_call_count > 0
        ):
            failures.append("active_view_not_reused")
        if current_user_count is not None and current_user_count != 1:
            failures.append("current_user_not_exactly_once")
    return failures


def _message_dicts(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    return [
        {
            "role": message.role,
            "content": message.content,
            **({"tool_call_id": message.tool_call_id} if message.tool_call_id else {}),
            **({"event_ref": message.event_ref} if message.event_ref else {}),
        }
        for message in messages
    ]


def _dicts_to_messages(items: list[dict[str, Any]]) -> list[LLMMessage]:
    return [
        LLMMessage(
            role=item["role"],
            content=item["content"],
            tool_call_id=item.get("tool_call_id"),
            event_ref=item.get("event_ref"),
        )
        for item in items
    ]


def _stable_history_hash(messages: list[LLMMessage]) -> str:
    payload = [{"role": item.role, "content": item.content} for item in messages]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _canonical_hash(messages: list[LLMMessage]) -> str:
    payload = [
        {
            "role": item.role,
            "content": item.content,
            "tool_call_id": item.tool_call_id,
            "event_ref": item.event_ref,
        }
        for item in messages
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    report: dict[str, dict[str, float | int]] = {}
    for variant in _VARIANTS:
        group = [row for row in rows if row["variant"] == variant]
        if not group:
            continue
        report[variant] = {
            "runs": len(group),
            "passed": sum(row["passed"] for row in group),
            "pass_rate": statistics.mean(float(row["passed"]) for row in group),
            "mean_raw_history_tokens": statistics.mean(row["raw_history_tokens"] for row in group),
            "mean_final_tokens": statistics.mean(row["final_tokens"] for row in group),
            "mean_compaction_ratio": statistics.mean(row["compaction_ratio"] for row in group),
            "mean_final_pressure_ratio": statistics.mean(row["final_pressure_ratio"] for row in group),
            "mean_hard_constraint_recall": statistics.mean(row["hard_constraint_recall"] for row in group),
            "mean_recent_raw_recall": statistics.mean(row["recent_raw_recall"] for row in group),
            "orphan_actions": sum(row["orphan_actions"] for row in group),
            "orphan_observations": sum(row["orphan_observations"] for row in group),
            "summary_calls": sum(row["summary_call_count"] for row in group),
            "summary_total_tokens": sum(row["summary_total_tokens"] for row in group),
            "semantic_duration_ms": sum(row["semantic_duration_ms"] for row in group),
        }
    return report


def _write_results(output: Path, metadata: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    with (output / "raw.jsonl").open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    aggregate = _aggregate(rows)
    report = {"metadata": metadata, "aggregate": aggregate, "rows": len(rows)}
    (output / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Context Policy B1 Benchmark",
        "",
        f"- Runner revision: `{metadata['runner_revision']}`",
        f"- Fixture SHA256: `{metadata['fixture_sha256']}`",
        f"- Semantic mode: `{metadata['semantic']['mode']}`",
        "",
        "| variant | runs | pass rate | mean raw tokens | mean final tokens | compaction ratio | final pressure | summary calls | summary tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant, values in aggregate.items():
        lines.append(
            f"| {variant} | {values['runs']} | {values['pass_rate']:.3f} | "
            f"{values['mean_raw_history_tokens']:.1f} | {values['mean_final_tokens']:.1f} | "
            f"{values['mean_compaction_ratio']:.3f} | {values['mean_final_pressure_ratio']:.3f} | "
            f"{values['summary_calls']} | {values['summary_total_tokens']} |"
        )
    failed = [row for row in rows if not row["passed"]]
    if failed:
        lines += ["", "## Contract failures", ""]
        lines.extend(
            f"- `{row['case_id']}` / `{row['variant']}`: {', '.join(row['failures'])}"
            for row in failed
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _git_revision(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay Forge Context Policy against fixed histories")
    parser.add_argument("--repo", default=str(_ROOT))
    parser.add_argument("--manifest", default=str(_MANIFEST))
    parser.add_argument("--output", default="evals/results/context_policy_benchmark")
    parser.add_argument("--semantic-mode", choices=("fixture", "live"), default="fixture")
    parser.add_argument("--config", default=None)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    report = run_benchmark(
        repo=args.repo,
        manifest=args.manifest,
        output=args.output,
        semantic_mode=args.semantic_mode,
        config_path=args.config,
        allow_dirty=args.allow_dirty,
    )
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
