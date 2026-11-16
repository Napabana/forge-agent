from __future__ import annotations

from multiprocessing import get_context
from pathlib import Path

import pytest

from agent.session import (
    ChatSessionConflict,
    ChatSessionState,
    ChatSessionRepoMismatch,
    PendingRoundState,
)
from agent.session_store import JsonChatSessionStore, utc_now
from agent.task import Action, ActionType
from config.schema import AppConfig
from entry.chat import ChatSession
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.agent.max_steps = 5
    config.agent.budget_tokens = 40_000
    config.agent.log_dir = str(tmp_path / "logs")
    config.context.history_window = 20
    return config


def _registry() -> ToolRegistry:
    return ToolRegistry().register(NoopTool("shell"))


def _session(
    tmp_path: Path,
    backend,
    store: JsonChatSessionStore,
    session_id: str | None = None,
) -> ChatSession:
    config = _config(tmp_path)
    return ChatSession(
        backend=backend,
        registry=_registry(),
        config=config,
        repo_path=str(tmp_path),
        log_dir=config.agent.log_dir,
        session_store=store,
        session_id=session_id,
        stream=False,
    )


def _save_session_in_process(root, session_id, title, ready, release, outcomes):
    """独立进程先加载相同 revision，收到统一信号后再竞争保存。"""
    store = JsonChatSessionStore(root)
    state = store.load(session_id)
    state.title = title
    ready.put(title)
    release.wait()
    try:
        store.save(state)
        outcomes.put("saved")
    except ChatSessionConflict:
        outcomes.put("conflict")


def test_json_store_round_trip_and_latest(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    state.title = "durable chat"
    state.history = [{"role": "user", "content": "remember me"}]
    store.save(state)

    loaded = store.load(state.session_id)
    latest = store.latest_for_repo(tmp_path)

    assert loaded.title == "durable chat"
    assert loaded.history[0]["content"] == "remember me"
    assert latest is not None
    assert latest.session_id == state.session_id
    assert not list(store.state_path(state).parent.glob("*.tmp"))


def test_stale_session_save_is_rejected(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    first = store.load(state.session_id)
    stale = store.load(state.session_id)

    first.title = "first writer"
    store.save(first)
    stale.title = "stale writer"

    with pytest.raises(ChatSessionConflict):
        store.save(stale)

    assert store.load(state.session_id).title == "first writer"


def test_two_processes_cannot_overwrite_the_same_session_revision(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    context = get_context("spawn")
    ready, outcomes, release = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_save_session_in_process, args=(str(store.root), state.session_id, title, ready, release, outcomes)) for title in ("first process", "second process")]

    for process in processes:
        process.start()
    assert {ready.get(timeout=10), ready.get(timeout=10)} == {"first process", "second process"}
    release.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted((outcomes.get(timeout=10), outcomes.get(timeout=10))) == ["conflict", "saved"]
    assert store.load(state.session_id).title in {"first process", "second process"}


def test_v1_session_is_migrated_on_read(tmp_path):
    state = ChatSessionState(
        session_id="legacy",
        repo_path=str(tmp_path),
        repo_key="repo",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    raw = state.to_dict()
    raw["version"] = 1
    raw.pop("revision")
    raw.pop("compaction_checkpoints")

    migrated = ChatSessionState.from_dict(raw)

    assert migrated.version == 2
    assert migrated.revision == 0
    assert migrated.compaction_checkpoints == []


def test_session_store_redacts_secrets_on_disk(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    state.history = [{"role": "user", "content": "Authorization: Bearer very-secret-token"}]
    state.title = "api_key=sk-abcdefghijklmnop"

    store.save(state)

    persisted = store.state_path(state).read_text(encoding="utf-8")
    assert "very-secret-token" not in persisted
    assert "sk-abcdefghijklmnop" not in persisted
    assert persisted.count("[REDACTED]") == 2


def test_chat_session_resumes_history_and_statistics(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    first = _session(
        tmp_path,
        MockBackend([Action(ActionType.FINISH, "done", message="created module")]),
        store,
    )
    assert first.run_round("create the module")
    session_id = first.session_id

    received: list[list[str]] = []

    class RecordingBackend(MockBackend):
        def complete(self, messages, tools):
            received.append([message.content for message in messages])
            return super().complete(messages, tools)

    resumed = _session(
        tmp_path,
        RecordingBackend([
            Action(ActionType.FINISH, "done", message="added tests"),
        ]),
        store,
        session_id=session_id,
    )
    assert resumed.round_count == 1
    assert resumed.total_steps == 1
    assert resumed.run_round("add tests")
    assert resumed.round_count == 2
    assert "created module" in " ".join(received[0])

    saved = store.load(session_id)
    assert saved.pending_round is None
    assert len(saved.rounds) == 2
    assert saved.rounds[-1].status == "success"
    assert Path(saved.rounds[-1].log_path).parent.name == "rounds"
    assert saved.usage.llm_calls == 2
    assert saved.usage.input_tokens == 200
    assert saved.usage.output_tokens == 100
    assert saved.usage.total_tokens == 300
    assert saved.rounds[-1].usage.llm_calls == 1


def test_old_session_tokens_remain_explicitly_unattributed(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    raw = state.to_dict()
    raw.pop("usage")
    raw["total_tokens"] = 321

    loaded = ChatSessionState.from_dict(raw)

    assert loaded.usage.unattributed_tokens == 321
    assert loaded.usage.total_tokens == 321


def test_keyboard_interrupt_is_checkpointed(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")

    class InterruptingBackend(MockBackend):
        def complete(self, messages, tools):
            raise KeyboardInterrupt()

    session = _session(tmp_path, InterruptingBackend([]), store)
    with pytest.raises(KeyboardInterrupt):
        session.run_round("start a risky edit")

    state = store.load(session.session_id)
    assert state.pending_round is None
    assert state.round_count == 1
    assert state.rounds[-1].status == "interrupted"
    assert "KeyboardInterrupt" in (state.rounds[-1].error or "")


def test_stale_pending_round_is_recovered_without_replay(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(tmp_path)
    state.round_count = 1
    state.history = [{"role": "user", "content": "half finished edit"}]
    state.pending_round = PendingRoundState(
        round_number=1,
        task_id="deadbeef",
        user_input="half finished edit",
        started_at=utc_now(),
        log_path=str(tmp_path / "round.jsonl"),
    )
    store.save(state)

    resumed = _session(
        tmp_path,
        MockBackend([Action(ActionType.FINISH, "done", message="ok")]),
        store,
        session_id=state.session_id,
    )

    assert "deadbeef" in (resumed.recovery_warning or "")
    saved = store.load(state.session_id)
    assert saved.pending_round is None
    assert saved.rounds[-1].status == "interrupted"
    assert "Tool side effects may be partial" in saved.history[-1]["content"]


def test_session_cannot_resume_in_another_repository(tmp_path):
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    state = store.create(first_repo)

    config = _config(tmp_path)
    with pytest.raises(ChatSessionRepoMismatch):
        ChatSession(
            backend=MockBackend([]),
            registry=_registry(),
            config=config,
            repo_path=str(second_repo),
            log_dir=config.agent.log_dir,
            session_store=store,
            session_id=state.session_id,
            stream=False,
        )


def test_clear_rename_and_new_session_are_persisted(tmp_path):
    store = JsonChatSessionStore(tmp_path / "logs" / "chat")
    session = _session(
        tmp_path,
        MockBackend([Action(ActionType.FINISH, "done", message="ok")]),
        store,
    )
    session.run_round("remember this")
    old_id = session.session_id

    session.rename("implementation work")
    session.clear_history()
    assert store.load(old_id).title == "implementation work"
    assert store.load(old_id).history == []

    new_id = session.start_new_session()
    assert new_id != old_id
    assert session.round_count == 0
    assert store.load(new_id).history == []
