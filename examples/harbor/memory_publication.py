"""Promote one untrusted proposal against a host-attested verifier snapshot."""
import os
import re
import tempfile
from pathlib import Path

from project_memory.identity import ProjectIdentityResolver
from examples.harbor.result_policy import is_benchmark_outcome
from examples.harbor.trace_evidence import analyze_harbor
from project_memory.store import MemoryStore, source_hashes
from project_memory.transfer import export_verified_memory, import_verified_memory
from project_memory.writer import MemoryWriter


def promote_candidate(*, writer: MemoryWriter, proposal: dict, verified_repo: Path,
                      run_id: str, base_commit: str, snapshot_sha256: str,
                      verifier_snapshot_sha256: str, changed_files: frozenset[str],
                      harbor_result: Path, verifier_details: Path, run_usage: dict) -> str | None:
    """Only call with identity and snapshot digests established by the host.

    The writer must target a private staging generation for this repository.
    Commit and publish that generation separately after finishing the trial.
    """
    if (
        not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256)
        or verifier_snapshot_sha256 != snapshot_sha256
        or (run_usage.get("run_status") != "success" and not is_benchmark_outcome(run_usage))
        or proposal.get("source_run_ids") != [run_id]
        or proposal.get("source_commit") != base_commit
        or proposal.get("status") != "pending"
        or proposal.get("type") not in ("feature", "experience")
    ):
        return None
    verdict = analyze_harbor(harbor_result, verifier_details=verifier_details)
    if (verdict["exception_type"] is not None or verdict["verifier_reward"] != 1
            or verdict["verifier_details_parse_status"] != "ok"):
        return None
    files = proposal.get("source_files")
    if (not isinstance(files, list) or not files or len(files) > 30
            or any(not isinstance(path, str) for path in files)
            or not set(files) <= changed_files
            or source_hashes(verified_repo, files) != proposal.get("source_file_hashes")):
        return None

    # Recreate the row through the validating writer; don't import pending or activity files.
    candidate = writer.propose(
        kind=proposal["type"], title=proposal.get("title"), content=proposal.get("content"),
        related_files=files, tags=proposal.get("tags"), run_id=run_id, repo=str(verified_repo),
        commit=base_commit,
    )
    if any(row["memory_id"] == candidate and row.get("confidence") == "verified"
           for row in writer.store.read_records()):
        return candidate
    writer.resolve(candidate, status="approved", repo=str(verified_repo), verified=True)
    return candidate


def publish_verified_trial(*, target: Path, previous: bytes | None,
                           repo_id: str, proposals: list[dict], verified_repo: Path,
                           **verification) -> int:
    """Stage a new generation. Readers never see candidates or a partial generation."""
    if target.exists():
        raise FileExistsError(target)
    if len(proposals) > 200:
        raise ValueError("too many trial memory candidates")
    with tempfile.TemporaryDirectory(prefix=".memory-stage-", dir=target.parent) as staging:
        store = MemoryStore(ProjectIdentityResolver.resolve(verified_repo), staging)
        if previous is not None:
            import_verified_memory(store, repo_id, previous)
        existing = {row["memory_id"] for row in store.read_records()}
        writer = MemoryWriter(store)
        promoted = {
            candidate for row in proposals
            if (candidate := promote_candidate(
                writer=writer, proposal=row, verified_repo=verified_repo, **verification,
            )) is not None
        }
        promoted -= existing
        if not promoted:
            return 0
        blob = Path(staging) / "published.json"
        with blob.open("wb") as stream:
            stream.write(export_verified_memory(store, repo_id))
            stream.flush()
            os.fsync(stream.fileno())
        # Link, rather than replace, so resumed or concurrent dispatch cannot overwrite it.
        target.hardlink_to(blob)
        return len(promoted)
