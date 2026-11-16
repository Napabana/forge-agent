"""Build machine-readable and Markdown ablation summaries from JSONL runs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from evals.harness import EvalResult, summarize


def build_report(input_path: str | Path, output_prefix: str | Path) -> dict[str, dict[str, float | int]]:
    groups: dict[str, list[EvalResult]] = defaultdict(list)
    for line in Path(input_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            result = EvalResult(**json.loads(line))
            groups[result.variant].append(result)
    report = {variant: summarize(results) for variant, results in sorted(groups.items())}
    prefix = Path(output_prefix)
    prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metrics = next(iter(report.values()), {}).keys()
    lines = ["# Evaluation ablation", "", "| variant | " + " | ".join(metrics) + " |", "| --- | " + " | ".join("---:" for _ in metrics) + " |"]
    for variant, summary in report.items():
        lines.append("| " + variant + " | " + " | ".join(str(summary[key]) for key in metrics) + " |")
    prefix.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="JSONL file containing EvalResult objects")
    parser.add_argument("output_prefix", help="path prefix for .json and .md reports")
    args = parser.parse_args()
    build_report(args.input, args.output_prefix)


if __name__ == "__main__":
    main()
