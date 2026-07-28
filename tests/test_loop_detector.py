from __future__ import annotations

import subprocess

from agent.core import Agent, AgentConfig
from agent.event_log import EventLog
from agent.loop_detector import (
    LoopDetector,
    LoopSeverity,
    action_fingerprint,
    snapshot_repository,
)
from agent.task import (
    Action,
    ActionType,
    EventType,
    Observation,
    ObservationStatus,
    RunStatus,
    Task,
    ToolCall,
)
from llm.base import MockBackend
from tools.base import NoopTool, ToolRegistry


def action(name, params=None):
    return Action(
        ActionType.TOOL_CALL,
        "thought",
        ToolCall(name, params or {}),
    )


def observation(output="same", status=ObservationStatus.SUCCESS):
    return Observation(status, output, "shell")


def feed(detector, actions, *, outputs=None, repos=None, tests=None):
    signal = None
    for index, item in enumerate(actions):
        signal = detector.observe(
            item,
            observation((outputs or ["same"] * len(actions))[index]),
            repo_state=(repos or ["repo"] * len(actions))[index],
            test_state=(tests or [None] * len(actions))[index],
        )
    return signal


def test_action_fingerprint_is_canonical_for_parameter_order_and_paths():
    first = action("Shell", {"env": {"B": 2, "A": 1}, "path": "a\\b"})
    second = action("shell", {"path": "a/b", "env": {"A": 1, "B": 2}})

    assert action_fingerprint(first) == action_fingerprint(second)


def test_detects_aaa_then_escalates_only_after_fresh_repetition():
    detector = LoopDetector(repeats=3, max_period=3)
    repeated = action("shell", {"cmd": "pwd"})

    first = feed(detector, [repeated] * 3)
    assert first is not None
    assert first.severity == LoopSeverity.REFLECT
    assert first.period == 1
    assert detector.buffered_steps == 0

    second = feed(detector, [repeated] * 3)
    assert second is not None
    assert second.severity == LoopSeverity.TERMINATE
    assert second.occurrence == 2


def test_detects_ababab_period_two():
    detector = LoopDetector(repeats=3, max_period=3)
    pattern = [action("shell", {"cmd": "a"}), action("shell", {"cmd": "b"})]

    signal = feed(detector, pattern * 3, outputs=["a", "b"] * 3)

    assert signal is not None
    assert signal.period == 2
    assert signal.severity == LoopSeverity.REFLECT


def test_detects_abcabcabc_period_three():
    detector = LoopDetector(repeats=3, max_period=3)
    pattern = [
        action("shell", {"cmd": "a"}),
        action("shell", {"cmd": "b"}),
        action("shell", {"cmd": "c"}),
    ]

    signal = feed(detector, pattern * 3, outputs=["a", "b", "c"] * 3)

    assert signal is not None
    assert signal.period == 3


def test_changed_observation_counts_as_progress():
    detector = LoopDetector(repeats=3)
    repeated = action("shell", {"cmd": "poll"})

    signal = feed(detector, [repeated] * 3, outputs=["1", "2", "3"])

    assert signal is None


def test_repository_or_test_state_change_counts_as_progress():
    repeated = action("test")
    repo_detector = LoopDetector(repeats=3)
    test_detector = LoopDetector(repeats=3)

    assert feed(
        repo_detector,
        [repeated] * 3,
        repos=["before", "after", "after"],
    ) is None
    assert feed(
        test_detector,
        [repeated] * 3,
        tests=["error", "error", "success"],
    ) is None


def test_buffer_is_bounded():
    detector = LoopDetector(repeats=3, max_period=3, capacity=12)
    for index in range(100):
        detector.observe(
            action("shell", {"cmd": str(index)}),
            observation(str(index)),
            repo_state="repo",
        )

    assert detector.buffered_steps <= 12


def test_agent_reflects_then_terminates_and_logs_structured_events(tmp_path):
    repeated = action("shell", {"cmd": "echo hi"})
    backend = MockBackend([repeated] * 10)
    registry = ToolRegistry().register(NoopTool("shell"))
    agent = Agent(
        backend,
        registry,
        AgentConfig(loop_detection_window=3, loop_detection_max_period=3),
    )
    task = Task(
        task_id="loop-structured",
        description="loop",
        repo_path=str(tmp_path),
        max_steps=10,
    )

    with EventLog.create(task, log_dir=str(tmp_path / "logs")) as log:
        def fail_if_replayed():
            raise AssertionError("loop detection must not replay EventLog")

        log.get_actions = fail_if_replayed
        result = agent.run(task, log)
        events = log.replay()

    loop_events = [event for event in events if event.event_type == EventType.LOOP_DETECTED]
    reflections = [event for event in events if event.event_type == EventType.REFLECTION]

    assert result.status == RunStatus.GAVE_UP
    assert result.steps_taken == 6
    assert [event.payload["severity"] for event in loop_events] == ["reflect", "terminate"]
    assert all(event.payload["period"] == 1 for event in loop_events)
    assert len(reflections) == 1
    assert reflections[0].payload["reason"] == "loop_detected"

def test_git_snapshot_changes_when_same_untracked_file_content_changes(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    target = tmp_path / "work.py"
    target.write_text("value = 1\n")
    first = snapshot_repository(tmp_path)

    target.write_text("value = 2\n")
    second = snapshot_repository(tmp_path)

    assert first != second