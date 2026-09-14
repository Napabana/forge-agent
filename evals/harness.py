"""Small, dependency-free harness for fixed local coding tasks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


_MANIFEST = Path(__file__).parent / "fixtures" / "tasks.json"


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    prompt: str
    files: dict[str, str]
    verifier: str
    target_files: tuple[str, ...] = ()
    target_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    variant: str
    passed: bool
    false_finish: bool
    tokens: int
    cost_usd: float
    latency_seconds: float
    tool_calls: int = 0
    human_intervention: bool = False


def load_cases(path: str | Path = _MANIFEST) -> list[EvalCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [EvalCase(
        case_id=item["id"], category=item["category"], prompt=item["prompt"],
        files=item["files"], verifier=item["verifier"],
        target_files=tuple(item.get("target_files", ())),
        target_symbols=tuple(item.get("target_symbols", ())),
    ) for item in raw]


def materialize_case(case: EvalCase, target: str | Path) -> Path:
    root = Path(target).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for relative, content in case.files.items():
        path = (root / relative).resolve()
        if root not in path.parents:
            raise ValueError(f"fixture path escapes target: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def verify_case(case: EvalCase, repo: str | Path, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, "-c", case.verifier], cwd=Path(repo), capture_output=True,
        text=True, timeout=timeout, check=False, env=env,
    )


def summarize(results: list[EvalResult]) -> dict[str, float | int]:
    total = len(results)
    solved = sum(result.passed for result in results)
    latencies = sorted(result.latency_seconds for result in results)

    def percentile(percent: float) -> float:
        if not latencies:
            return 0.0
        index = max(0, min(len(latencies) - 1, int(percent * len(latencies) + 0.999999) - 1))
        return latencies[index]

    return {
        "runs": total, "solved": solved,
        "pass_at_1": solved / total if total else 0.0,
        "false_finish_rate": sum(result.false_finish for result in results) / total if total else 0.0,
        "tokens_per_solved": sum(result.tokens for result in results) / solved if solved else 0.0,
        "cost_usd_per_solved": sum(result.cost_usd for result in results) / solved if solved else 0.0,
        "p50_latency_seconds": percentile(0.50), "p95_latency_seconds": percentile(0.95),
        "tool_calls": sum(result.tool_calls for result in results),
        "human_intervention_rate": sum(result.human_intervention for result in results) / total if total else 0.0,
    }


def result_to_dict(result: EvalResult) -> dict[str, object]:
    return asdict(result)
