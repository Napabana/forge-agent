"""Recompute Repo Map Agent exploration metrics from frozen Trace v2 files.

Historical P2 rows counted only file_read/file_view for `files_read` and
`first_target_read_step`. Real runs also used shell commands heavily. This
reader keeps the legacy metric but adds explicit path access through shell
commands without rewriting historical raw results.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_TRACE_MARKER = "evals/results/repo_map_agent_ablation/traces/"


def resolve_trace_path(raw_path: str | None, root: Path = _ROOT) -> Path | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    if path.is_file():
        return path
    normalized = raw_path.replace("\\", "/")
    if _TRACE_MARKER in normalized:
        relative = normalized.split(_TRACE_MARKER, 1)[1]
        candidate = root / _TRACE_MARKER / relative
        if candidate.is_file():
            return candidate
    return None


def analyze_trace(trace_path: str | Path, *, repo_path: str | Path, target_files: tuple[str, ...]) -> dict[str, Any]:
    repo = Path(repo_path).resolve()
    targets = {Path(item).as_posix() for item in target_files}
    repo_files = [path for path in repo.rglob("*") if path.is_file() and ".git" not in path.parts]
    relative_by_path = {path: path.relative_to(repo).as_posix() for path in repo_files}
    file_tool_files: set[str] = set()
    shell_files: set[str] = set()
    first_file_tool_target: int | None = None
    first_target_access: int | None = None

    for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event_type") != "action":
            continue
        payload = event.get("payload", {})
        step = int(payload.get("step") or 0)
        call = ((payload.get("action") or {}).get("tool_call") or {})
        tool = call.get("name")
        params = call.get("params") or {}
        if tool in {"file_read", "file_view"}:
            raw = params.get("path")
            if raw:
                candidate = Path(str(raw))
                absolute = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
                try:
                    relative = absolute.relative_to(repo).as_posix()
                except ValueError:
                    relative = None
                if relative:
                    file_tool_files.add(relative)
                    if relative in targets:
                        first_file_tool_target = first_file_tool_target or step
                        first_target_access = first_target_access or step
        elif tool == "shell":
            command = str(params.get("cmd") or "")
            for path, relative in relative_by_path.items():
                if relative in command or str(path) in command:
                    shell_files.add(relative)
                    if relative in targets:
                        first_target_access = first_target_access or step

    explicit = file_tool_files | shell_files
    return {
        "legacy_first_target_read_step": first_file_tool_target,
        "first_target_access_step": first_target_access,
        "file_tool_files_read": len(file_tool_files),
        "shell_referenced_files": len(shell_files),
        "explicit_files_accessed": len(explicit),
        "target_accessed": first_target_access is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", default="evals/results/repo_map_agent_ablation_real_v1/raw.jsonl")
    parser.add_argument("--manifest", default="evals/fixtures/repo_map_agent_cases.json")
    parser.add_argument("--output", default="../forge-agent-evals/repo-map-agent-trace-analysis.jsonl")
    args = parser.parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    cases = {item["id"]: item for item in manifest["cases"]}
    rows = []

    for line in Path(args.raw).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        trace = resolve_trace_path(raw.get("trace_path"))
        if trace is None:
            rows.append({**raw, "trace_analysis_error": "trace_not_found"})
            continue
        normalized = str(trace).replace("\\", "/")
        repo_root = normalized.split("/traces/", 1)[0] + "/repos/"
        repo = Path(repo_root) / raw["variant"] / f"repeat-{raw['repeat']}" / raw["case_id"]
        case = cases[raw["case_id"]]
        rows.append({
            "case_id": raw["case_id"],
            "variant": raw["variant"],
            "repeat": raw["repeat"],
            **analyze_trace(trace, repo_path=repo, target_files=tuple(case["target_files"])),
        })

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
