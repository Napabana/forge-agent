"""Bind grading and Memory publication to the same sealed snapshot bytes."""
import base64
import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath

from examples.harbor.memory_publication import publish_verified_trial


def seal_snapshot(base_commit: str, changed: Mapping[str, bytes]) -> bytes:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base_commit):
        raise ValueError("invalid frozen commit")
    if not 0 < len(changed) <= 200:
        raise ValueError("invalid changed-file count")
    files = {}
    size = 0
    for name, content in sorted(changed.items()):
        path = PurePosixPath(name)
        if (path.is_absolute() or not path.parts or path.as_posix() != name
                or any(part in {"..", ".git"} for part in path.parts)
                or any(c in name for c in ("\\", "\x00", "\n", "\r")) or not isinstance(content, bytes)):
            raise ValueError("unsafe snapshot entry")
        size += len(content)
        if size > 4 * 1024 * 1024:
            raise ValueError("snapshot exceeds byte limit")
        files[name] = base64.b64encode(content).decode("ascii")
    return json.dumps({"base_commit": base_commit, "files": files}, sort_keys=True,
                      separators=(",", ":")).encode()


def grade_and_publish(*, target: Path, previous: bytes | None, repo_id: str,
                      proposals: list[dict], run_id: str, base_commit: str,
                      changed: Mapping[str, bytes], run_usage: dict,
                      verify: Callable[[bytes], tuple[str, dict, dict]]) -> int:
    """The trusted independent verifier must run on the supplied bytes.

    Trial code never supplies either receipt. Unsupported changes (e.g. file
    deletions or mode changes) must be rejected by the caller at capture time.
    """
    stable = dict(changed)
    snapshot = seal_snapshot(base_commit, stable)
    digest = hashlib.sha256(snapshot).hexdigest()
    consumed, harbor_result, details = verify(snapshot)
    if consumed != digest:
        raise ValueError("independent verifier did not attest the sealed snapshot")
    if details.get("eval_trustworthy") is False:
        return 0
    with tempfile.TemporaryDirectory(prefix=".verified-snapshot-", dir=target.parent) as path:
        staged = Path(path)
        verified = staged / "source"
        for name, body in stable.items():
            file = verified / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(body)
        result_file, details_file = staged / "trial.json", staged / "verifier.json"
        result_file.write_text(json.dumps(harbor_result), encoding="utf-8")
        details_file.write_text(json.dumps(details), encoding="utf-8")
        return publish_verified_trial(
            target=target, previous=previous, repo_id=repo_id, proposals=proposals,
            verified_repo=verified, run_id=run_id, base_commit=base_commit,
            snapshot_sha256=digest, verifier_snapshot_sha256=consumed,
            changed_files=frozenset(stable), harbor_result=result_file,
            verifier_details=details_file, run_usage=run_usage,
        )


def publish_captured_trial(*, result_file: Path, target: Path, previous: bytes | None,
                           repo_id: str, expected_base: str, verify) -> int:
    """Run the independent stage only for a completed trial with a promotable capture."""
    verdict = json.loads(result_file.read_text(encoding="utf-8"))
    metadata = (verdict.get("agent_result") or {}).get("metadata") or {}
    if (verdict.get("exception_info") is not None or metadata.get("memory_capture_status") != "captured"
            or ((verdict.get("verifier_result") or {}).get("rewards") or {}).get("reward") != 1):
        return 0
    usage = metadata.get("forge_usage") or {}
    task_id = usage.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("memory capture has no correlated parent trace")
    trial = result_file.resolve().parent
    capsule = (trial / "agent/forge/memory-capture.json").resolve()
    if not capsule.is_relative_to(trial):
        raise ValueError("memory capture leaves its trial directory")
    with capsule.open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("memory capture exceeds wire limit")
    capture = json.loads(raw)
    if capture.get("base_commit") != expected_base:
        raise ValueError("memory capture does not match the frozen base")
    proposals = capture["proposals"]
    if not proposals or not capture["files"]:
        return 0
    changed = {name: base64.b64decode(value, validate=True) for name, value in capture["files"].items()}
    return grade_and_publish(
        target=target, previous=previous, repo_id=repo_id, proposals=proposals, run_id=task_id,
        base_commit=expected_base, changed=changed, run_usage=usage, verify=verify,
    )
