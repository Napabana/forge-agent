"""Display fields derived from a finished run, after event observation has stopped."""
import subprocess
from pathlib import Path


def repo_branch(repo):
    try:
        result = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                                capture_output=True, timeout=2, encoding="utf-8", errors="replace")
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def patch_stat(patch):
    if not patch:
        return None
    try:
        result = subprocess.run(["git", "apply", "--numstat", "-z", "--recount", "-"],
                                input=(patch.rstrip("\n") + "\n").encode("utf-8"), capture_output=True,
                                cwd=Path.cwd().anchor, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    lines = []
    for record in result.stdout.decode("utf-8", "replace").split("\0"):
        if record:
            added, deleted, path = record.split("\t", 2)
            lines.append(f"{path}  +{added} -{deleted}" if added != "-" else f"{path}  (binary)")
    return "\n".join(lines)


def summary_fields(result, elapsed, tools, *, show_tokens, tests=None):
    rows = {"Status": result.status.value.upper(), "Time": f"{elapsed:.1f}s", "Steps": result.steps_taken}
    usage = getattr(result, "usage", None)
    if usage and usage.llm_calls:
        rows["LLM calls"] = usage.llm_calls
    if show_tokens:
        if result.total_tokens or getattr(result, "usage_complete", None) is True:
            rows["Tokens"] = f"{result.total_tokens:,}"
        if usage and (usage.input_tokens or usage.output_tokens or getattr(result, "usage_complete", None)):
            rows["Token usage"] = f"input {usage.input_tokens:,} · output {usage.output_tokens:,}"
            if usage.unknown_cache_read_calls < usage.llm_calls:
                rows["Token usage"] += f" · cached {usage.cached_tokens:,}"
        if getattr(result, "usage_complete", None) is False:
            rows["Usage coverage"] = "partial or estimated"
    if tools:
        rows["Tools"] = " · ".join(f"{count} {status}" for status, count in sorted(tools.items()))
    if tests:
        rows["Test runs"] = " · ".join(
            f"{count} {outcome.replace('_', ' ')}" for outcome, count in sorted(tests.items())
        )
    for key in ("acceptance_status", "acceptance_error", "delivery_status", "termination_reason",
                "resource_reason", "error"):
        rows[key.replace("_status", "").replace("_", " ").capitalize()] = getattr(result, key, None)
    artifact = getattr(result, "worktree", None)
    if artifact:
        rows.update(Branch=artifact.branch, Worktree=artifact.path or artifact.disposition.value,
                    Changes="\n".join(artifact.changed_files), Warning=artifact.warning)
        rows["Worktree changes"] = f"{artifact.uncommitted_count} uncommitted · {artifact.commit_count} commit(s)"
    rows["Diff stat"] = patch_stat(getattr(result, "patch", None))
    rows["Warnings"] = "\n".join(getattr(result, "completion_warnings", ()))
    return rows
