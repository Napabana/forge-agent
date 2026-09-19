"""Deterministic promotion gate and explicit project Skill deployment."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from experience.schema import (
    CandidateStatus,
    EvaluationRecord,
    EvaluationRole,
    PromotionDecision,
    PromotionStatus,
    SkillCandidate,
    stable_hash,
    utc_now,
)
from experience.store import CandidateStore

_SAFE_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _project_skill_file(
    repo_root: Path,
    skill_name: str,
) -> Path:
    if not _SAFE_SKILL_NAME.fullmatch(skill_name):
        raise ValueError(
            f"invalid project Skill name: {skill_name!r}"
        )
    skill_file = (
        repo_root
        / ".agents"
        / "skills"
        / skill_name
        / "SKILL.md"
    )
    try:
        skill_file.resolve(strict=False).relative_to(repo_root)
    except ValueError as exc:
        raise ValueError(
            "project Skill target escaped repository boundary"
        ) from exc
    for parent in (
        repo_root / ".agents",
        repo_root / ".agents" / "skills",
        skill_file.parent,
        skill_file,
    ):
        if parent.is_symlink():
            raise FileExistsError(
                "refusing to use a symlinked project Skill target"
            )
    return skill_file


@dataclass(frozen=True)
class PromotionGateConfig:
    min_evidence_count: int = 2
    max_step_overhead: int = 4
    max_token_overhead_ratio: float = 0.50
    max_token_overhead_absolute: int = 2000
    required_roles: tuple[EvaluationRole, ...] = (
        EvaluationRole.TARGET,
        EvaluationRole.SHOULD_TRIGGER,
        EvaluationRole.SHOULD_NOT_TRIGGER,
        EvaluationRole.NON_REGRESSION,
    )

    def __post_init__(self) -> None:
        if self.min_evidence_count < 1:
            raise ValueError("min_evidence_count must be >= 1")
        if (
            self.max_step_overhead < 0
            or self.max_token_overhead_ratio < 0
            or self.max_token_overhead_absolute < 0
        ):
            raise ValueError("promotion overhead bounds must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_roles"] = [role.value for role in self.required_roles]
        return payload


class PromotionGate:
    def __init__(self, config: PromotionGateConfig | None = None) -> None:
        self.config = config or PromotionGateConfig()

    def evaluate(
        self,
        candidate: SkillCandidate,
        record: EvaluationRecord,
    ) -> PromotionDecision:
        reasons: list[str] = []
        status = PromotionStatus.PASS

        if (
            record.candidate_id != candidate.candidate_id
            or record.candidate_version != candidate.candidate_version
            or record.candidate_hash != candidate.content_hash
        ):
            status = PromotionStatus.EVALUATION_FAILED
            reasons.append(
                "evaluation record is stale or belongs to a different candidate version/hash"
            )
        elif any(case.evaluation_failed for case in record.cases):
            status = PromotionStatus.EVALUATION_FAILED
            reasons.extend(
                (
                    f"evaluation infrastructure failed for {case.task_id}: "
                    f"{case.failure_reason or 'unknown'}"
                )
                for case in record.cases
                if case.evaluation_failed
            )
        else:
            evidence_count = len({
                (
                    item.run_id,
                    item.task_id,
                    item.trace_sha256,
                    item.trial_id,
                )
                for item in candidate.source_trajectories
            })
            if evidence_count < self.config.min_evidence_count:
                status = PromotionStatus.INSUFFICIENT_EVIDENCE
                reasons.append(
                    f"source evidence count {evidence_count} "
                    f"< required {self.config.min_evidence_count}"
                )

        if status is PromotionStatus.PASS:
            present = {case.role for case in record.cases}
            missing = [
                role.value
                for role in self.config.required_roles
                if role not in present
            ]
            if missing:
                status = PromotionStatus.INSUFFICIENT_EVIDENCE
                reasons.append(
                    f"missing required evaluation roles: {', '.join(missing)}"
                )

        if status is PromotionStatus.PASS:
            for case in record.cases:
                label = f"{case.task_id}/r{case.repetition}"
                if (
                    not case.candidate_required_graders_passed
                    or not case.candidate_success
                ):
                    reasons.append(f"candidate outcome failed for {label}")
                if case.baseline_success and not case.candidate_success:
                    reasons.append(
                        f"candidate regressed a baseline-success case: {label}"
                    )
                if (
                    case.role
                    in {EvaluationRole.TARGET, EvaluationRole.SHOULD_TRIGGER}
                    and not case.candidate_loaded
                ):
                    reasons.append(
                        f"candidate was not loaded when required: {label}"
                    )
                if (
                    case.role is EvaluationRole.SHOULD_NOT_TRIGGER
                    and case.candidate_loaded
                ):
                    reasons.append(
                        f"candidate triggered on a should-not-trigger case: {label}"
                    )
                if not case.candidate_process_passed:
                    reasons.append(
                        f"candidate process grader failed for {label}"
                    )
                if (
                    case.candidate_steps - case.baseline_steps
                    > self.config.max_step_overhead
                ):
                    reasons.append(
                        f"step overhead exceeded for {label}"
                    )
                token_limit = (
                    case.baseline_tokens
                    + self.config.max_token_overhead_absolute
                    + int(
                        case.baseline_tokens
                        * self.config.max_token_overhead_ratio
                    )
                )
                if case.candidate_tokens > token_limit:
                    reasons.append(
                        f"token overhead exceeded for {label}"
                    )
            if reasons:
                status = PromotionStatus.REJECT

        config_payload = self.config.to_dict()
        digest = stable_hash({
            "status": status.value,
            "candidate_id": candidate.candidate_id,
            "candidate_version": candidate.candidate_version,
            "candidate_hash": candidate.content_hash,
            "evaluation_record_id": record.record_id,
            "reasons": reasons,
            "gate_config": config_payload,
        })[:16]
        return PromotionDecision(
            decision_id=f"decision-{digest}",
            status=status,
            candidate_id=candidate.candidate_id,
            candidate_version=candidate.candidate_version,
            candidate_hash=candidate.content_hash,
            evaluation_record_id=record.record_id,
            reasons=(
                tuple(reasons)
                if reasons
                else ("all deterministic promotion checks passed",)
            ),
            gate_config=config_payload,
            created_at=utc_now(),
        )


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError(
            "SKILL.md frontmatter is not terminated"
        ) from exc
    metadata = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return metadata, "\n".join(lines[end + 1:]).strip()


def _render_promoted_skill(
    candidate: SkillCandidate,
    decision: PromotionDecision,
    *,
    skill_version: int,
) -> str:
    metadata = {
        "name": candidate.skill_name,
        "description": candidate.description,
        "forge_evolution": {
            "managed": True,
            "skill_version": skill_version,
            "candidate_id": candidate.candidate_id,
            "candidate_version": candidate.candidate_version,
            "candidate_hash": candidate.content_hash,
            "evaluation_record_id": (
                decision.evaluation_record_id
            ),
            "decision_id": decision.decision_id,
            "parent_skill_version": (
                candidate.parent_skill_version
            ),
            "parent_skill_hash": candidate.parent_skill_hash,
        },
    }
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return (
        f"---\n{frontmatter}\n---\n"
        f"{candidate.instructions.strip()}\n"
    )


def _semantic_from_markdown(
    raw: str,
) -> tuple[str, int | None, dict[str, Any] | None]:
    metadata, instructions = _parse_frontmatter(raw)
    name = str(metadata.get("name", "")).strip().lower()
    description = " ".join(
        str(metadata.get("description", "")).split()
    )
    content_hash = SkillCandidate.semantic_hash(
        name,
        description,
        instructions,
    )
    evolution = metadata.get("forge_evolution")
    if (
        not isinstance(evolution, dict)
        or evolution.get("managed") is not True
    ):
        return content_hash, None, None
    try:
        version = int(evolution["skill_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "managed Skill has invalid evolution version metadata"
        ) from exc
    return content_hash, version, evolution


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class PromotionManager:
    """Explicitly deploy approved candidates into the existing project SkillCatalog path."""

    def __init__(
        self,
        repo_root: str | Path,
        store: CandidateStore,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        if store.repo_root != self.repo_root:
            raise ValueError(
                "promotion store must belong to the same repository"
            )
        self.store = store

    def promote(
        self,
        candidate: SkillCandidate,
        decision: PromotionDecision,
    ) -> Path:
        if decision.status is not PromotionStatus.PASS:
            raise ValueError(
                "candidate cannot be promoted unless the deterministic gate passed"
            )
        if (
            decision.candidate_id != candidate.candidate_id
            or decision.candidate_version
            != candidate.candidate_version
            or decision.candidate_hash != candidate.content_hash
        ):
            raise ValueError(
                "promotion decision does not match current candidate"
            )
        stored_decision = self.store.load_decision(
            decision.decision_id
        )
        if (
            stored_decision != decision
            or stored_decision.status
            is not PromotionStatus.PASS
        ):
            raise ValueError(
                "promotion decision is not the persisted PASS decision"
            )
        record = self.store.load_evaluation(
            decision.evaluation_record_id
        )
        if (
            record.candidate_id != candidate.candidate_id
            or record.candidate_version
            != candidate.candidate_version
            or record.candidate_hash != candidate.content_hash
        ):
            raise ValueError(
                "promotion evaluation is stale for "
                "current candidate version/hash"
            )
        if (
            self.store.status(
                candidate.candidate_id,
                candidate.candidate_version,
            )
            is not CandidateStatus.APPROVED
        ):
            raise ValueError(
                "candidate must be explicitly recorded as APPROVED "
                "before promotion"
            )

        skill_file = _project_skill_file(
            self.repo_root,
            candidate.skill_name,
        )
        skill_dir = skill_file.parent
        skill_version = 1
        if skill_dir.exists() and not skill_file.exists():
            raise FileExistsError(
                "refusing to populate an existing Skill directory "
                "without SKILL.md"
            )
        if skill_file.exists():
            raw = skill_file.read_text(encoding="utf-8")
            current_hash, current_version, evolution = (
                _semantic_from_markdown(raw)
            )
            if evolution is None or current_version is None:
                raise FileExistsError(
                    "refusing to overwrite a user-managed Skill "
                    "without Forge evolution provenance"
                )
            if (
                str(evolution.get("candidate_hash") or "")
                != current_hash
            ):
                raise ValueError(
                    "managed Skill content no longer matches "
                    "its recorded candidate hash"
                )
            if (
                candidate.parent_skill_name
                != candidate.skill_name
                or candidate.parent_skill_version
                != current_version
                or candidate.parent_skill_hash
                != current_hash
            ):
                raise ValueError(
                    "candidate parent Skill version/hash "
                    "does not match promotion target"
                )
            skill_version = current_version + 1
        elif candidate.parent_skill_name is not None:
            raise ValueError(
                "candidate declares a parent Skill but "
                "the promotion target does not exist"
            )

        promoted = _render_promoted_skill(
            candidate,
            decision,
            skill_version=skill_version,
        )
        self.store.save_approved_snapshot(
            candidate.skill_name,
            skill_version,
            promoted,
        )
        skill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text(skill_file, promoted)
        self.store.set_status(
            candidate.candidate_id,
            candidate.candidate_version,
            CandidateStatus.PROMOTED,
        )
        self.store.audit("skill_candidate_promoted", {
            "candidate_id": candidate.candidate_id,
            "candidate_version": candidate.candidate_version,
            "candidate_hash": candidate.content_hash,
            "evaluation_record_id": (
                decision.evaluation_record_id
            ),
            "decision_id": decision.decision_id,
            "skill": candidate.skill_name,
            "skill_version": skill_version,
            "target": (
                f".agents/skills/{candidate.skill_name}/SKILL.md"
            ),
        })
        return skill_file

    def rollback(
        self,
        skill_name: str,
        skill_version: int,
    ) -> Path:
        skill_file = _project_skill_file(
            self.repo_root,
            skill_name,
        )
        if not skill_file.is_file():
            raise ValueError(
                f"cannot rollback missing Skill: {skill_name}"
            )
        current = skill_file.read_text(encoding="utf-8")
        _, _, evolution = _semantic_from_markdown(current)
        if evolution is None:
            raise ValueError(
                "refusing to rollback a user-managed Skill"
            )
        snapshot = self.store.load_approved_snapshot(
            skill_name,
            skill_version,
        )
        _, snapshot_version, snapshot_evolution = (
            _semantic_from_markdown(snapshot)
        )
        if (
            snapshot_evolution is None
            or snapshot_version != skill_version
        ):
            raise ValueError(
                "approved rollback snapshot has invalid provenance"
            )
        _atomic_text(skill_file, snapshot)
        self.store.audit("skill_rollback", {
            "skill": skill_name,
            "skill_version": skill_version,
            "target": (
                f".agents/skills/{skill_name}/SKILL.md"
            ),
        })
        return skill_file
