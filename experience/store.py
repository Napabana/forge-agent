"""Project-bounded immutable storage for Skill candidates and evaluation evidence."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from experience.schema import (
    CandidateStatus,
    EvaluationRecord,
    PromotionDecision,
    PromotionStatus,
    SkillCandidate,
    utc_now,
)

_SAFE_COMPONENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_CANDIDATE_JSON_BYTES = 256 * 1024
_MAX_SKILL_BYTES = 128 * 1024
_MAX_EVALUATION_JSON_BYTES = 2 * 1024 * 1024
_MAX_AUDIT_LINE_BYTES = 16 * 1024


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _check_component(value: str, *, field_name: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(value):
        raise ValueError(f"unsafe {field_name}: {value!r}")
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


class CandidateStore:
    def __init__(
        self,
        repo_root: str | Path,
        store_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        if not self.repo_root.is_dir():
            raise ValueError(
                "candidate store repo_root must be an existing directory"
            )
        raw = (
            Path(store_dir)
            if store_dir is not None
            else Path(".forge-agent") / "experience"
        )
        self.root = (
            raw if raw.is_absolute() else self.repo_root / raw
        ).resolve()
        try:
            self.root.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(
                "candidate store must remain inside the repository boundary"
            ) from exc
        self.root.mkdir(parents=True, exist_ok=True)

    def _candidate_dir(
        self,
        candidate_id: str,
        version: int,
    ) -> Path:
        _check_component(candidate_id, field_name="candidate id")
        if version < 1:
            raise ValueError("candidate version must be >= 1")
        return (
            self.root
            / "candidates"
            / candidate_id
            / f"v{version:03d}"
        )

    def _state_path(
        self,
        candidate_id: str,
        version: int,
    ) -> Path:
        return (
            self._candidate_dir(candidate_id, version)
            / "state.json"
        )

    def save_candidate(
        self,
        candidate: SkillCandidate,
    ) -> Path:
        target = self._candidate_dir(
            candidate.candidate_id,
            candidate.candidate_version,
        )
        if target.exists():
            raise FileExistsError(
                f"candidate version already exists: {target}"
            )
        candidate_json = _json_bytes(candidate.to_dict())
        skill_bytes = candidate.skill_markdown().encode("utf-8")
        if len(candidate_json) > _MAX_CANDIDATE_JSON_BYTES:
            raise ValueError(
                "candidate metadata exceeds bounded artifact size"
            )
        if len(skill_bytes) > _MAX_SKILL_BYTES:
            raise ValueError(
                "candidate SKILL.md exceeds bounded artifact size"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(
            tempfile.mkdtemp(
                prefix=".candidate-",
                dir=target.parent,
            )
        )
        try:
            (temp / "skill").mkdir()
            (temp / "candidate.json").write_bytes(candidate_json)
            (temp / "skill" / "SKILL.md").write_bytes(skill_bytes)
            (temp / "state.json").write_bytes(_json_bytes({
                "status": CandidateStatus.DRAFT.value,
                "updated_at": utc_now(),
            }))
            os.replace(temp, target)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
        self.audit("skill_candidate_created", {
            "candidate_id": candidate.candidate_id,
            "candidate_version": candidate.candidate_version,
            "candidate_hash": candidate.content_hash,
            "pattern_id": candidate.pattern_id,
            "source_count": len(candidate.source_trajectories),
            "source_run_ids": [
                item.run_id[:256]
                for item in candidate.source_trajectories[:32]
            ],
            "source_trace_hashes": [
                item.trace_sha256
                for item in candidate.source_trajectories[:32]
            ],
        })
        return target

    def load_candidate(
        self,
        candidate_id: str,
        version: int,
    ) -> SkillCandidate:
        target = self._candidate_dir(candidate_id, version)
        metadata = target / "candidate.json"
        skill = target / "skill" / "SKILL.md"
        if not metadata.is_file() or not skill.is_file():
            raise ValueError("candidate artifact is incomplete")
        if (
            metadata.stat().st_size > _MAX_CANDIDATE_JSON_BYTES
            or skill.stat().st_size > _MAX_SKILL_BYTES
        ):
            raise ValueError(
                "candidate artifact exceeds bounded size"
            )
        try:
            raw = json.loads(
                metadata.read_text(encoding="utf-8")
            )
            candidate = SkillCandidate.from_dict(raw)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"corrupt candidate metadata: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if (
            candidate.candidate_id != candidate_id
            or candidate.candidate_version != version
        ):
            raise ValueError(
                "candidate identity does not match storage path"
            )
        expected_hash = SkillCandidate.semantic_hash(
            candidate.skill_name,
            candidate.description,
            candidate.instructions,
        )
        if expected_hash != candidate.content_hash:
            raise ValueError("candidate content hash mismatch")
        try:
            skill_text = skill.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"corrupt candidate SKILL.md: {exc}"
            ) from exc
        if skill_text != candidate.skill_markdown():
            raise ValueError(
                "candidate SKILL.md does not match immutable metadata"
            )
        return candidate

    def status(
        self,
        candidate_id: str,
        version: int,
    ) -> CandidateStatus:
        path = self._state_path(candidate_id, version)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return CandidateStatus(str(raw["status"]))
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"corrupt candidate state: {exc}"
            ) from exc

    def set_status(
        self,
        candidate_id: str,
        version: int,
        status: CandidateStatus,
    ) -> None:
        current = self.status(candidate_id, version)
        allowed = {
            CandidateStatus.DRAFT: {
                CandidateStatus.EVALUATING,
            },
            CandidateStatus.EVALUATING: {
                CandidateStatus.DRAFT,
                CandidateStatus.REJECTED,
                CandidateStatus.APPROVED,
            },
            CandidateStatus.REJECTED: {
                CandidateStatus.EVALUATING,
            },
            CandidateStatus.APPROVED: {
                CandidateStatus.PROMOTED,
                CandidateStatus.EVALUATING,
            },
            CandidateStatus.PROMOTED: {
                CandidateStatus.EVALUATING,
            },
        }
        if (
            status is not current
            and status not in allowed[current]
        ):
            raise ValueError(
                "invalid candidate status transition: "
                f"{current.value} -> {status.value}"
            )
        _atomic_write(
            self._state_path(candidate_id, version),
            _json_bytes({
                "status": status.value,
                "updated_at": utc_now(),
            }),
        )

    def save_evaluation(
        self,
        record: EvaluationRecord,
    ) -> Path:
        path = (
            self.root
            / "evaluations"
            / (
                f"{_check_component(record.record_id, field_name='evaluation id')}"
                ".json"
            )
        )
        if path.exists():
            raise FileExistsError(
                f"evaluation record already exists: {path}"
            )
        payload = _json_bytes(record.to_dict())
        if len(payload) > _MAX_EVALUATION_JSON_BYTES:
            raise ValueError(
                "evaluation record exceeds bounded artifact size"
            )
        _atomic_write(path, payload)
        self.audit("skill_candidate_evaluated", {
            "candidate_id": record.candidate_id,
            "candidate_version": record.candidate_version,
            "candidate_hash": record.candidate_hash,
            "evaluation_record_id": record.record_id,
            "suite_id": record.suite_id,
        })
        return path

    def load_evaluation(
        self,
        record_id: str,
    ) -> EvaluationRecord:
        path = (
            self.root
            / "evaluations"
            / (
                f"{_check_component(record_id, field_name='evaluation id')}"
                ".json"
            )
        )
        if (
            not path.is_file()
            or path.stat().st_size > _MAX_EVALUATION_JSON_BYTES
        ):
            raise ValueError(
                "evaluation record is missing or exceeds bounded size"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return EvaluationRecord.from_dict(raw)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"corrupt evaluation record: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def save_decision(
        self,
        decision: PromotionDecision,
    ) -> Path:
        path = (
            self.root
            / "decisions"
            / (
                f"{_check_component(decision.decision_id, field_name='decision id')}"
                ".json"
            )
        )
        if path.exists():
            raise FileExistsError(
                f"promotion decision already exists: {path}"
            )
        _atomic_write(
            path,
            _json_bytes(decision.to_dict()),
        )
        status_map = {
            PromotionStatus.PASS: CandidateStatus.APPROVED,
            PromotionStatus.REJECT: CandidateStatus.REJECTED,
            PromotionStatus.INSUFFICIENT_EVIDENCE: (
                CandidateStatus.DRAFT
            ),
            PromotionStatus.EVALUATION_FAILED: (
                CandidateStatus.DRAFT
            ),
        }
        self.set_status(
            decision.candidate_id,
            decision.candidate_version,
            status_map[decision.status],
        )
        event = {
            PromotionStatus.PASS: "skill_candidate_approved",
            PromotionStatus.REJECT: "skill_candidate_rejected",
            PromotionStatus.INSUFFICIENT_EVIDENCE: (
                "skill_candidate_insufficient_evidence"
            ),
            PromotionStatus.EVALUATION_FAILED: (
                "skill_candidate_evaluation_failed"
            ),
        }[decision.status]
        self.audit(event, {
            "candidate_id": decision.candidate_id,
            "candidate_version": decision.candidate_version,
            "candidate_hash": decision.candidate_hash,
            "evaluation_record_id": (
                decision.evaluation_record_id
            ),
            "decision_id": decision.decision_id,
            "promotion_status": decision.status.value,
        })
        return path

    def load_decision(
        self,
        decision_id: str,
    ) -> PromotionDecision:
        path = (
            self.root
            / "decisions"
            / (
                f"{_check_component(decision_id, field_name='decision id')}"
                ".json"
            )
        )
        if (
            not path.is_file()
            or path.stat().st_size > _MAX_CANDIDATE_JSON_BYTES
        ):
            raise ValueError(
                "promotion decision is missing or exceeds bounded size"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return PromotionDecision.from_dict(raw)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise ValueError(
                f"corrupt promotion decision: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def save_approved_snapshot(
        self,
        skill_name: str,
        skill_version: int,
        markdown: str,
    ) -> Path:
        _check_component(
            skill_name,
            field_name="skill name",
        )
        if skill_version < 1:
            raise ValueError(
                "skill version must be >= 1"
            )
        data = markdown.encode("utf-8")
        if len(data) > _MAX_SKILL_BYTES:
            raise ValueError(
                "approved Skill exceeds bounded artifact size"
            )
        path = (
            self.root
            / "approved"
            / skill_name
            / f"v{skill_version:03d}"
            / "SKILL.md"
        )
        if path.exists():
            raise FileExistsError(
                f"approved Skill snapshot already exists: {path}"
            )
        _atomic_write(path, data)
        return path

    def load_approved_snapshot(
        self,
        skill_name: str,
        skill_version: int,
    ) -> str:
        _check_component(
            skill_name,
            field_name="skill name",
        )
        path = (
            self.root
            / "approved"
            / skill_name
            / f"v{skill_version:03d}"
            / "SKILL.md"
        )
        if (
            not path.is_file()
            or path.stat().st_size > _MAX_SKILL_BYTES
        ):
            raise ValueError(
                "approved Skill snapshot is missing "
                "or exceeds bounded size"
            )
        return path.read_text(encoding="utf-8")

    def audit(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        _check_component(
            event_type,
            field_name="evolution event type",
        )
        record = {
            "event_type": event_type,
            "timestamp": utc_now(),
            "payload": payload,
        }
        line = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        encoded = line.encode("utf-8")
        if len(encoded) > _MAX_AUDIT_LINE_BYTES:
            raise ValueError(
                "evolution audit event exceeds bounded metadata size"
            )
        path = self.root / "evolution_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
