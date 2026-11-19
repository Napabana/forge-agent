"""Python pilot provenance: compare Agent source with the actual pytest session."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


PACKAGE_ROOTS = {"click": "src/click", "httpx": "httpx"}
TASK_PACKAGES = {"pallets__click": "click", "encode__httpx": "httpx"}
PROBE_NAME = "forge_verifier_provenance"


def source_snapshot(repo: Path, package: str) -> dict:
    repo = repo.resolve()
    package_root = PACKAGE_ROOTS[package]

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], timeout=10)

    files = {}
    root = repo / package_root
    if root.resolve() != root:
        raise ValueError("unsafe source package root")
    for path in root.rglob("*.py"):
        if path.resolve() != path or not path.is_file():
            raise ValueError("unsafe source path in verifier scope")
        files[path.relative_to(repo).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if files.get(f"{package_root}/__init__.py") is None:
        raise ValueError("missing source package")
    return {"package": package, "base_commit": git("rev-parse", "HEAD").decode().strip(),
            "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "file_count": len(files), "python": sys.executable}


def pytest_sessionstart(session):
    package = os.environ["FORGE_PROVENANCE_PACKAGE"]
    session._forge_source = source_snapshot(Path.cwd(), package)


def pytest_sessionfinish(session, exitstatus):
    before = session._forge_source
    package = before["package"]
    repo = Path.cwd().resolve()
    root = repo / PACKAGE_ROOTS[package]
    after = source_snapshot(repo, package)
    imports = {}
    for name, module in tuple(sys.modules.items()):
        if name != package and not name.startswith(f"{package}."):
            continue
        filename = getattr(module, "__file__", None)
        if isinstance(filename, str):
            path = Path(filename).resolve()
            imports[name] = {"path": str(path), "in_scope": path.is_relative_to(root),
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                             if path.is_relative_to(root) and path.is_file() else None}
    receipt = {"before": before, "after": after, "imports": imports, "pytest_exit_status": int(exitstatus)}
    with Path(os.environ["FORGE_PROVENANCE_RECEIPT"]).open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, sort_keys=True)


def instrument_task(source: Path, target: Path, package: str) -> Path:
    """Only wrap the verifier of a private copy; keep its script and test data unchanged."""
    if package not in PACKAGE_ROOTS or not (source / "tests/test.sh").is_file():
        raise ValueError("unsupported provenance task")
    shutil.copytree(source, target)
    tests = target / "tests"
    runner = tests / "test.sh"
    for name in ("__forge_original_test.sh", f"{PROBE_NAME}.py"):
        if (tests / name).exists():
            raise ValueError("verifier probe name collision")
    shutil.copyfile(runner, tests / "__forge_original_test.sh")
    shutil.copyfile(Path(__file__), tests / f"{PROBE_NAME}.py")
    runner.write_text(
        '#!/bin/bash\nset -eu\n'
        'test_dir="$(cd "$(dirname "$0")" && pwd)"\n'
        'probe_dir="$(mktemp -d)"\n'
        f'cp "$test_dir/{PROBE_NAME}.py" "$probe_dir/"\n'
        'export PYTHONPATH="$probe_dir${PYTHONPATH:+:$PYTHONPATH}"\n'
        f'export PYTEST_PLUGINS="{PROBE_NAME}${{PYTEST_PLUGINS:+,$PYTEST_PLUGINS}}"\n'
        f'export FORGE_PROVENANCE_PACKAGE={package}\n'
        'export FORGE_PROVENANCE_RECEIPT=/logs/verifier/forge-source-provenance.json\n'
        'exec /bin/bash "$test_dir/__forge_original_test.sh" "$@"\n',
        encoding="utf-8",
    )
    return target


def provenance_matches(expected, receipt, *, package: str, base_commit: str, exit_code: int) -> bool:
    if not isinstance(expected, dict) or not isinstance(receipt, dict):
        return False
    imports = receipt.get("imports")
    digest = expected.get("sha256")
    pytest_status = receipt.get("pytest_exit_status")
    return (
        expected.get("package") == package and expected.get("base_commit") == base_commit
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and isinstance(expected.get("python"), str) and expected["python"].startswith("/")
        and type(expected.get("file_count")) is int and expected["file_count"] >= 1
        and expected == receipt.get("before") == receipt.get("after")
        and type(exit_code) is type(pytest_status) is int and pytest_status == exit_code
        and isinstance(imports, dict) and package in imports
        and all(isinstance(record, dict) and record.get("in_scope") is True
                and isinstance(record.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                for record in imports.values())
    )


if __name__ == "__main__":
    print(json.dumps(source_snapshot(Path.cwd(), sys.argv[1]), sort_keys=True))
