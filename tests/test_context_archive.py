import json

import pytest

from agent.task import Task
from context.archive import ArchiveRewrite, ContextArchive
from durable_runtime import DurableRuntimeStore, StoreFormatError
from durable_runtime.run_manager import DurableRun
from project_memory.identity import ProjectIdentity
from tools.archive_read import ArchiveReadTool


def test_archive_captures_history_and_projects_summary_atomically(tmp_path, record_property):
    identity = ProjectIdentity("archive-test", "path", str(tmp_path))
    messages = [{"role": "user", "content": "x" * 512, "event_ref": "raw-one"}]
    with DurableRuntimeStore(identity, tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        run.archive = ContextArchive(store, run.owner.scope_id, inline_limit=32)
        run.create(Task("archive", str(tmp_path)), "cli")
        compressed = [{"role": "assistant", "content": "Summary of the original"}]
        run.checkpoint(2, "context_prepared", {"history": messages, "model_history": compressed})
        checkpoint = run.load_checkpoint()
        assert checkpoint["canonical_archive_revision"] == checkpoint["model_archive_revision"] == 1
        records = run.archive.records()
        assert [row["kind"] for row in records] == ["raw", "summary"]
        raw_id, summary_id = [row["archive_id"] for row in records]
        assert json.loads(records[1]["source_ids_json"]) == [raw_id]
        tool = ArchiveReadTool(run.archive, lambda: False)
        page = json.loads(tool.execute({"id": raw_id, "offset": 0, "limit": 20}).output)
        assert page["has_more"] and page["next_offset"] == 20
        recalled = json.loads(tool.execute({"id": raw_id, "limit": 4096}).output)
        assert json.loads(recalled["content"]) == messages[0]
        record_property("archive_recall_correct", 1)
        backup = store.backup(tmp_path / "archive-snapshot.db")
        assert (backup.with_name(backup.name + ".blobs") / records[0]["payload_digest"]).is_file()
        path = run.archive._blob(records[0]["payload_digest"])
        path.write_text("damaged", encoding="utf-8")
        with pytest.raises(StoreFormatError, match="digest"):
            run.archive.read(summary_id, 0, 200)
        record_property("archive_tamper_detected", 1)
        other = DurableRun(store, "other", "other-scope")
        other_archive = ContextArchive(store, other.owner.scope_id)
        with pytest.raises(StoreFormatError, match="current scope"):
            other_archive.read(raw_id, 0, 20)


def test_archive_import_is_idempotent_and_rewrite_validates_retained_source(tmp_path, record_property):
    with DurableRuntimeStore(ProjectIdentity("project", "path", str(tmp_path)), tmp_path) as store:
        run = DurableRun(store, "run", "scope")
        archive = ContextArchive(store, "scope", session_id="legacy")
        history = [{"role": "user", "content": "original"}] * 2
        with run.transaction() as db:
            record = archive.import_history(db, history, "legacy")
        with run.transaction() as db:
            assert archive.import_history(db, history, "legacy") == record
            revision, ids = archive.projection(db, "model_view")
            assert len(set(ids)) == 2
        with pytest.raises(StoreFormatError, match="retained source"):
            with run.transaction() as db:
                archive.apply_rewrite(db, ArchiveRewrite(revision, tuple(ids), ({"content": "changed"},), (ids[0],)))
        assert archive.restore_model_view(revision) == history
        assert [item["kind"] for item in archive.records()] == ["raw", "raw", "import"]
        record_property("archive_recall_correct", 1)


def test_runtime_import_chat_uses_saved_session_identity(tmp_path):
    from click.testing import CliRunner
    from agent.session_store import JsonChatSessionStore
    from entry.cli import cli

    logs, runtime = tmp_path / "logs", tmp_path / "runtime"
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    sessions = JsonChatSessionStore(logs / "chat")
    state = sessions.create(repo)
    state.history = [{"role": "user", "content": "older request"}]
    sessions.save(state)
    config = tmp_path / "config.yaml"
    config.write_text("agent:\n  log_dir: " + json.dumps(str(logs)) +
                      "\ndurable_runtime:\n  enabled: true\n  root: " + json.dumps(str(runtime)), encoding="utf-8")
    cli_runner = CliRunner()
    prefix = ["--config", str(config), "runtime", "--repo"]
    args = [*prefix, str(repo), "import-chat", state.session_id]
    first, second = cli_runner.invoke(cli, args), cli_runner.invoke(cli, args)
    assert first.exit_code == second.exit_code == 0, (first.exception, second.exception)
    assert first.output == second.output
    denied = cli_runner.invoke(cli, [*prefix, str(other), "import-chat", state.session_id])
    assert denied.exit_code != 0 and "different project" in denied.output


def test_archive_bundle_copies_transitive_sources_without_replacing_destination(tmp_path, record_property):
    from click.testing import CliRunner
    from durable_runtime.run_manager import digest, encode
    from entry.cli import cli
    from project_memory.identity import ProjectIdentityResolver

    repo, runtime = tmp_path / "repo", tmp_path / "runtime"
    repo.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("durable_runtime:\n  enabled: true\n  root: " + json.dumps(str(runtime)), encoding="utf-8")
    identity = ProjectIdentityResolver.resolve(repo)
    source_history = [{"role": "user", "content": "x" * 70000}]
    view = [{"role": "assistant", "content": "source summary"}]
    destination_history = [{"role": "user", "content": "existing destination"}]
    with DurableRuntimeStore(identity, runtime) as store:
        source = DurableRun(store, "source-run", "source")
        source.create(Task("source", str(repo)), "cli")
        source.archive = ContextArchive(store, "source", run_id=source.run_id)
        source.checkpoint(2, "context_prepared", {"history": source_history, "model_history": view})
        summary_id = source.archive.model_archive_ids()[0]
        before = source.archive.records()
        with DurableRun(store, "dst", "destination").transaction() as db:
            ContextArchive(store, "destination").import_history(db, destination_history, "destination")
    runner = CliRunner()
    bundle = tmp_path / "bundle.json"
    args = ["--config", str(config), "runtime", "--repo", str(repo)]
    result = runner.invoke(cli, [*args, "archive-export", "source", str(bundle), "--id", summary_id])
    assert result.exit_code == 0, result.output
    assert runner.invoke(cli, [*args, "archive-export", "source", str(bundle), "--id", summary_id]).exit_code != 0
    package = json.loads(bundle.read_bytes())
    assert len(package["records"]) == 2
    loaded = runner.invoke(cli, [*args, "archive-import", "destination", str(bundle)])
    assert loaded.exit_code == 0, loaded.output
    receipt = json.loads(loaded.output)
    assert runner.invoke(cli, [*args, "archive-import", "destination", str(bundle)]).output == loaded.output
    other = tmp_path / "other"
    other.mkdir()
    wrong_project = runner.invoke(cli, [*args[:-1], str(other), "archive-import", "destination", str(bundle)])
    assert wrong_project.exit_code != 0 and "project mismatch" in wrong_project.output
    with DurableRuntimeStore(identity, runtime) as store:
        assert ContextArchive(store, "source").records() == before
        destination = ContextArchive(store, "destination")
        assert destination.restore_model_view(1) == destination_history
        copied_summary = destination.read(receipt["roots"][0], 0, 4096)
        assert copied_summary["archive_id"] != summary_id
        assert json.loads(copied_summary["content"]) == view[0]
        copied_source = destination.read(copied_summary["source_ids"][0], 0, 4096)
        assert copied_source["has_more"]
        with pytest.raises(StoreFormatError, match="current scope"):
            destination.read(summary_id, 0, 100)
        count = len(destination.records())
    package["records"][0]["sources"] = [package["roots"][0]]
    package["digest"] = digest(encode({key: value for key, value in package.items() if key != "digest"}))
    bundle.write_bytes(encode(package).encode())
    denied = runner.invoke(cli, [*args, "archive-import", "destination", str(bundle)])
    assert denied.exit_code != 0 and "source order" in denied.output
    with DurableRuntimeStore(identity, runtime) as store:
        assert len(ContextArchive(store, "destination").records()) == count
    record_property("archive_bundle_correct", 1)
