"""Schema for the task-level Coding Agent Evaluation Harness."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SUPPORTED_GRADERS = {"command", "file", "repository_state", "run_trace", "skill_selection", "recovery_motif"}


def _validate_id(value: str, *, field_name: str) -> None:
    if not value or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must match {_ID_RE.pattern}: {value!r}")


def _validate_fixture_path(path: str, *, field_name: str) -> None:
    candidate = PurePosixPath(path.replace("\\", "/"))
    if not path or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{field_name} must be a safe relative path: {path!r}")


@dataclass(frozen=True)
class GraderSpec:
    grader_id: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    required: bool = True

    def __post_init__(self) -> None:
        _validate_id(self.grader_id, field_name="grader_id")
        if self.kind not in _SUPPORTED_GRADERS:
            raise ValueError(f"unsupported grader kind: {self.kind!r}")
        if not isinstance(self.params, dict):
            raise ValueError("grader params must be an object")
        if self.kind == "command":
            command = self.params.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise ValueError(f"command grader {self.grader_id!r} requires a non-empty string list")
        elif self.kind == "file":
            path = self.params.get("path")
            if not isinstance(path, str):
                raise ValueError(f"file grader {self.grader_id!r} requires params.path")
            _validate_fixture_path(path, field_name=f"grader {self.grader_id} path")
        elif self.kind == "repository_state":
            if "changed" not in self.params:
                raise ValueError(f"repository_state grader {self.grader_id!r} requires params.changed")
        elif self.kind == "skill_selection":
            supported = {"expected_any", "forbidden"}
            unknown = sorted(set(self.params) - supported)
            if unknown:
                raise ValueError(
                    f"skill_selection grader {self.grader_id!r} has unsupported params: {unknown}"
                )
            if not any(key in self.params for key in supported):
                raise ValueError(
                    f"skill_selection grader {self.grader_id!r} requires expected_any or forbidden"
                )
            for key in supported:
                values = self.params.get(key, [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) and value for value in values
                ):
                    raise ValueError(
                        f"skill_selection grader {self.grader_id!r} {key} must be a string list"
                    )
        elif self.kind == "run_trace":
            supported = {
                "run_status", "termination_reason", "required_tool", "min_test_attempts",
                "min_tool_calls", "min_completion_rejections", "min_reflections",
            }
            unknown = sorted(set(self.params) - supported)
            if unknown:
                raise ValueError(f"run_trace grader {self.grader_id!r} has unsupported params: {unknown}")
        elif self.kind == "recovery_motif":
            required = {
                "failure_category",
                "recovery_strategy",
                "skill_name",
                "next_operation",
            }
            unknown = sorted(set(self.params) - required)
            missing = sorted(required - set(self.params))
            if unknown:
                raise ValueError(
                    f"recovery_motif grader {self.grader_id!r} has unsupported params: {unknown}"
                )
            if missing:
                raise ValueError(
                    f"recovery_motif grader {self.grader_id!r} is missing params: {missing}"
                )
            if not all(
                isinstance(self.params[key], str) and self.params[key].strip()
                for key in required
            ):
                raise ValueError(
                    f"recovery_motif grader {self.grader_id!r} params must be non-empty strings"
                )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GraderSpec":
        return cls(
            grader_id=str(raw.get("id", "")),
            kind=str(raw.get("kind", "")),
            params=dict(raw.get("params") or {}),
            required=bool(raw.get("required", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.grader_id, "kind": self.kind, "params": self.params, "required": self.required}

#一个评估的具体任务
@dataclass(frozen=True)
class EvalTask:
    task_id: str
    description: str
    files: dict[str, str]
    graders: tuple[GraderSpec, ...]
    reference_files: dict[str, str] = field(default_factory=dict)
    require_changes: bool = True
    require_tests: bool = False
    test_cmd: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.task_id, field_name="task_id")
        if not self.description.strip():
            raise ValueError(f"task {self.task_id!r} requires a description")
        if not self.files:
            raise ValueError(f"task {self.task_id!r} requires fixture files")
        for path, content in self.files.items():
            _validate_fixture_path(path, field_name=f"task {self.task_id} fixture path")
            if not isinstance(content, str):
                raise ValueError(f"fixture content must be text: {path}")
        for path, content in self.reference_files.items():
            _validate_fixture_path(path, field_name=f"task {self.task_id} reference path")
            if not isinstance(content, str):
                raise ValueError(f"reference content must be text: {path}")
        if not self.graders:
            raise ValueError(f"task {self.task_id!r} requires at least one grader")
        grader_ids = [grader.grader_id for grader in self.graders]
        duplicates = sorted({item for item in grader_ids if grader_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"task {self.task_id!r} has duplicate grader ids: {duplicates}")
        if any(not isinstance(tag, str) or not tag for tag in self.tags):
            raise ValueError(f"task {self.task_id!r} tags must be non-empty strings")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvalTask":
        return cls(
            task_id=str(raw.get("id", "")),
            description=str(raw.get("description", "")),
            files={str(k): str(v) for k, v in dict(raw.get("files") or {}).items()},
            reference_files={str(k): str(v) for k, v in dict(raw.get("reference_files") or {}).items()},
            require_changes=bool(raw.get("require_changes", True)),
            require_tests=bool(raw.get("require_tests", False)),
            test_cmd=(str(raw["test_cmd"]) if raw.get("test_cmd") is not None else None),
            tags=tuple(str(tag) for tag in raw.get("tags", ())),
            graders=tuple(GraderSpec.from_dict(item) for item in raw.get("graders", ())),
        )

#评估任务集
@dataclass(frozen=True)
class EvaluationSuite:
    suite_id: str
    tasks: tuple[EvalTask, ...]
    description: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported evaluation suite schema_version: {self.schema_version}")
        _validate_id(self.suite_id, field_name="suite_id")
        if not self.tasks:
            raise ValueError("evaluation suite must contain tasks")
        task_ids = [task.task_id for task in self.tasks]
        duplicates = sorted({item for item in task_ids if task_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate task id(s): {duplicates}")
        if int(self.defaults.get("max_steps", 20)) < 1:
            raise ValueError("defaults.max_steps must be >= 1")
        if int(self.defaults.get("budget_tokens", 40_000)) < 1:
            raise ValueError("defaults.budget_tokens must be >= 1")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EvaluationSuite":
        return cls(
            schema_version=int(raw.get("schema_version", 1)),
            suite_id=str(raw.get("suite_id", "")),
            description=str(raw.get("description", "")),
            defaults=dict(raw.get("defaults") or {}),
            tasks=tuple(EvalTask.from_dict(item) for item in raw.get("tasks", ())),
        )

    @classmethod
    def load(cls, path: str | Path) -> "EvaluationSuite":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("evaluation suite JSON must be an object")
        return cls.from_dict(raw)

#一次评估 绑定suite和task
@dataclass(frozen=True)
class TrialConfig:
    suite_id: str
    task_id: str
    variant: str
    repetition: int

    def __post_init__(self) -> None:
        _validate_id(self.suite_id, field_name="suite_id")
        _validate_id(self.task_id, field_name="task_id")
        _validate_id(self.variant, field_name="variant")
        if self.repetition < 1:
            raise ValueError("repetition must be >= 1")

    @property
    def trial_id(self) -> str:
        return f"{self.task_id}--{self.variant}--r{self.repetition:03d}"


@dataclass(frozen=True)
class GraderResult:
    grader_id: str
    kind: str
    passed: bool
    required: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

#记录agent如何完成任务 才有数据和baseline对比
@dataclass(frozen=True)
class TrialMetrics:
    steps: int = 0
    total_tokens: int = 0
    provider_usage: dict[str, Any] = field(default_factory=dict)
    wall_time_seconds: float = 0.0
    tool_call_count: int = 0
    test_attempt_count: int = 0
    completion_rejection_count: int = 0
    reflection_count: int = 0
    file_read_count: int = 0
    shell_call_count: int = 0
    plan_created_count: int = 0
    plan_revision_count: int = 0
    plan_step_completed_count: int = 0
    planning_skipped: bool = False
    failure_classified_count: int = 0
    recovery_selected_count: int = 0
    recovery_replan_count: int = 0
    recovery_exhausted_count: int = 0
    skill_discovered_count: int = 0
    skill_selected_count: int = 0
    skill_loaded_count: int = 0
    skill_reference_loaded_count: int = 0
    mcp_tool_discovered_count: int = 0
    mcp_tool_call_count: int = 0
    mcp_tool_failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

#后面真正做 A/B comparison 的基本数据单位
@dataclass(frozen=True)
class TrialResult:
    suite_id: str
    task_id: str
    trial_id: str
    variant: str
    repetition: int
    execution_status: str
    evidence_kind: str
    real_model_executed: bool
    run_status: str
    termination_reason: str | None
    acceptance_status: str
    success: bool
    metrics: TrialMetrics
    grader_results: tuple[GraderResult, ...]
    trace_ref: str | None = None
    patch_ref: str | None = None
    final_state_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = self.metrics.to_dict()
        payload["grader_results"] = [result.to_dict() for result in self.grader_results]
        return payload


@dataclass(frozen=True)
class EvalReport:
    """Serialized aggregate report; capability metrics are optional by evidence level."""

    suite_id: str
    execution_status: str
    real_model_executed: bool
    trial_count: int
    variants: tuple[str, ...]
    claim_boundary: str
    harness_validation: dict[str, Any] | None = None
    real_model_small_sample: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "suite_id": self.suite_id,
            "execution_status": self.execution_status,
            "real_model_executed": self.real_model_executed,
            "trial_count": self.trial_count,
            "variants": list(self.variants),
            "claim_boundary": self.claim_boundary,
        }
        if self.harness_validation is not None:
            payload["harness_validation"] = self.harness_validation
        if self.real_model_small_sample is not None:
            payload["real_model_small_sample"] = self.real_model_small_sample
        return payload
