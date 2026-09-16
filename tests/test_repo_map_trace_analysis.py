from __future__ import annotations

import json

from evals.repo_map_trace_analysis import analyze_trace


def test_trace_analysis_counts_file_tool_and_shell_explicit_target_access(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "target.py").write_text("x = 1\n")
    (repo / "other.py").write_text("y = 2\n")
    trace = tmp_path / "trace.jsonl"
    events = [
        {"event_type": "action", "payload": {"step": 1, "action": {"tool_call": {"name": "shell", "params": {"cmd": "cat target.py"}}}}},
        {"event_type": "action", "payload": {"step": 2, "action": {"tool_call": {"name": "file_read", "params": {"path": "other.py"}}}}},
    ]
    trace.write_text("\n".join(json.dumps(item) for item in events) + "\n")
    result = analyze_trace(trace, repo_path=repo, target_files=("target.py",))
    assert result["legacy_first_target_read_step"] is None
    assert result["first_target_access_step"] == 1
    assert result["shell_referenced_files"] == 1
    assert result["file_tool_files_read"] == 1
    assert result["explicit_files_accessed"] == 2
