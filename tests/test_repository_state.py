from __future__ import annotations

from context.repository_state import repository_content_fingerprint


def test_non_git_repository_content_fingerprint_ignores_runtime_logs(tmp_path):
    (tmp_path / "value.txt").write_text("stable\n", encoding="utf-8")
    before = repository_content_fingerprint(tmp_path)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    trace = log_dir / "run.jsonl"
    trace.write_text('{"event":"start"}\n', encoding="utf-8")
    trace.write_text('{"event":"start"}\n{"event":"step"}\n', encoding="utf-8")

    after = repository_content_fingerprint(tmp_path)

    assert after == before


def test_non_git_repository_content_fingerprint_still_tracks_real_files(tmp_path):
    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    before = repository_content_fingerprint(tmp_path)

    target.write_text("new\n", encoding="utf-8")
    after = repository_content_fingerprint(tmp_path)

    assert after != before
