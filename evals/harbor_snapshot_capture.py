"""Extract a conservative Memory candidate capsule from a trial workspace."""
import argparse
import base64
import json
import subprocess
from pathlib import Path, PurePosixPath

from project_memory.runtime import MemoryRuntime


def capture_candidate(repo: Path, memory_root: str) -> dict:
    runtime = MemoryRuntime(str(repo), root=memory_root, defer_promotion=True)
    proposals = [row for row in runtime.writer.pending() if row.get("status") == "pending"]
    if not proposals:
        return {"base_commit": runtime.base_commit, "files": {}, "proposals": []}
    if len(proposals) > 200:
        raise ValueError("too many Memory proposals")

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], timeout=20)

    if git("diff", "--summary", "HEAD", "--").strip():
        raise ValueError("snapshot capture does not support structural changes")
    files, size = {}, 0
    rows = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames").split(b"\0")
    for row in rows:
        if not row:
            continue
        if row[:3] not in (b" M ", b"M  ", b"MM "):
            raise ValueError("snapshot capture requires only modified tracked files")
        name = row[3:].decode("utf-8")
        parts = PurePosixPath(name).parts
        if (not parts or PurePosixPath(name).is_absolute() or ".." in parts
                or any(p.startswith(".") for p in parts)
                or {"tests", "test", "conftest.py"} & set(parts) or not name.endswith(".py")):
            raise ValueError("snapshot capture requires regular Python source changes")
        source = repo / name
        if (not source.resolve().is_relative_to(repo) or not source.is_file()
                or any(p.is_symlink() for p in (source, *source.parents) if p != repo)):
            raise ValueError("snapshot source is not a regular file within the workspace")
        with source.open("rb") as stream:
            body = stream.read(4 * 1024 * 1024 + 1)
        size += len(body)
        if len(files) == 200 or size > 4 * 1024 * 1024:
            raise ValueError("snapshot capture exceeds limits")
        files[name] = base64.b64encode(body).decode("ascii")
    return {"base_commit": runtime.base_commit, "files": files, "proposals": proposals}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.dumps(capture_candidate(args.repo.resolve(), args.memory_root), ensure_ascii=True)
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("Memory capture exceeds wire limit")
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(payload)


if __name__ == "__main__":
    main()
