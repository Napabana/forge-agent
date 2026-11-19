"""Grade sealed source bytes with the original verifier in a fresh Harbor job."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


_LOADER = r"""
import base64, hashlib, json, subprocess
from pathlib import Path, PurePosixPath
root = Path("/workspace").resolve()
snapshot = Path("/tmp/forge-snapshot/input.json").read_bytes()
assert hashlib.sha256(snapshot).hexdigest() == EXPECTED
document = json.loads(snapshot)
head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
assert head == document["base_commit"], "snapshot base commit mismatch"
for name, content in document["files"].items():
    parts = PurePosixPath(name).parts
    assert parts and not PurePosixPath(name).is_absolute() and ".." not in parts and ".git" not in parts
    # Don't let a patch replace tracked tests, pytest policy, or verifier inputs.
    assert not any(p.startswith(".") for p in parts)
    assert not {"tests", "test", "conftest.py"} & set(parts) and name.endswith(".py")
    dest = root / name
    assert dest.resolve().is_relative_to(root)
    assert not any(path.is_symlink() for path in (dest, *dest.parents) if path != root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(content, validate=True))
"""


def harbor_snapshot_verifier(*, frozen_task: Path, evidence_dir: Path, harbor: Path,
                             local_base_image: str | None = None):
    """Return a single-use verifier for regular Python source changes.

    The Docker build consumes the exact capsule, checks its hash and frozen
    Git base, and fails before entering the unchanged Harbor verifier on
    mismatch. The model container is not reused by this job.
    """
    if local_base_image is not None and not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[a-z0-9._-]+", local_base_image):
        raise ValueError("invalid trusted local base image reference")

    def verify(snapshot: bytes) -> tuple[str, dict, dict]:
        digest = hashlib.sha256(snapshot).hexdigest()
        evidence_dir.mkdir(parents=True, exist_ok=False)
        # A fresh job and build context; never alter the frozen dataset.
        task = evidence_dir / frozen_task.name
        shutil.copytree(frozen_task, task)
        environment = task / "environment"
        (environment / "__forge_snapshot.json").write_bytes(snapshot)
        (environment / "__forge_snapshot_apply.py").write_text(
            f"EXPECTED = {digest!r}\n" + _LOADER, encoding="utf-8",
        )
        dockerfile = environment / "Dockerfile"
        if local_base_image is not None:
            dockerfile.write_text(f"FROM {local_base_image}\nWORKDIR /workspace\n", encoding="utf-8")
        with dockerfile.open("a", encoding="utf-8") as stream:
            stream.write(
                "\nCOPY __forge_snapshot.json /tmp/forge-snapshot/input.json\n"
                "COPY __forge_snapshot_apply.py /tmp/forge-snapshot/apply.py\n"
                "RUN python3 -I /tmp/forge-snapshot/apply.py\n"
            )
        docker_config = evidence_dir / "docker-config"
        docker_config.mkdir(mode=0o700)
        env = {name: os.environ[name] for name in (
            "PATH", "HOME", "USER", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        ) if name in os.environ}
        env["DOCKER_CONFIG"] = str(docker_config)
        command = [str(harbor), "run", "--path", str(task), "-a", "nop", "--job-name", "verify",
                   "--jobs-dir", str(evidence_dir), "--n-concurrent", "1", "--n-attempts", "1",
                   "--max-retries", "0"]
        with (evidence_dir / "harbor.log").open("xb") as log:
            subprocess.run(command, cwd=evidence_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=1800, check=True)
        results = list((evidence_dir / "verify").glob("*/result.json"))
        if len(results) != 1:
            raise RuntimeError("independent snapshot verifier did not produce exactly one trial")
        result = json.loads(results[0].read_text(encoding="utf-8"))
        details = json.loads((results[0].parent / "verifier" / "reward-details.json")
                             .read_text(encoding="utf-8"))
        return digest, result, details
    return verify
