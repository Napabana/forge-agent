from agent.task import Event, EventType
from entry.event_renderer import RunEventRenderer


def _event(event_type, payload):
    return Event(event_type=event_type, task_id="task", payload=payload)


def test_shared_renderer_surfaces_run_lifecycle(capsys):
    renderer = RunEventRenderer(preview_lines=1)

    renderer(_event(EventType.ACTION, {
        "step": 2,
        "action": {
            "action_type": "tool_call",
            "thought": "private reasoning",
            "tool_call": {"name": "shell", "params": {"cmd": "pytest -q"}},
        },
    }))
    renderer(_event(EventType.OBSERVATION, {
        "step": 2,
        "observation": {
            "tool_name": "shell",
            "status": "success",
            "output": "10 passed\nsecond line",
        },
    }))
    renderer(_event(EventType.ACCEPTANCE, {
        "acceptance_status": "passed",
        "status": "passed",
    }))
    renderer(_event(EventType.DELIVERY, {
        "delivery_status": "delivered",
        "status": "delivered",
    }))

    output = capsys.readouterr().out
    assert "[Step 2] action=tool_call tool=shell params=pytest -q" in output
    assert "Observation [shell]: success" in output
    assert "10 passed" in output and "1 more lines" in output
    assert "Acceptance: passed" in output
    assert "Delivery: delivered" in output
    assert "private reasoning" not in output


def test_reasoning_visibility_is_independent_from_lifecycle(capsys):
    event = _event(EventType.ACTION, {
        "step": 1,
        "action": {
            "action_type": "finish",
            "thought": "visible reasoning",
            "message": "done",
            "tool_call": None,
        },
    })

    RunEventRenderer(show_reasoning=True)(event)

    output = capsys.readouterr().out
    assert "action=finish" in output
    assert "visible reasoning" in output


def test_shared_renderer_surfaces_control_flow_events(capsys):
    renderer = RunEventRenderer()

    renderer(_event(EventType.PLAN_CREATED, {
        "plan": {"version": 1, "goal": "Fix regression"},
    }))
    renderer(_event(EventType.COMPLETION_REJECTED, {
        "code": "FINAL_STATE_UNVERIFIED",
        "detail": "Repository content changed after the latest successful test.",
    }))
    renderer(_event(EventType.RECOVERY_SELECTED, {
        "category": "completion_rejected",
        "strategy": "rerun_test",
        "attempt": 2,
        "max_attempts": 4,
        "reason": "Fresh verification is required.",
    }))
    renderer(_event(EventType.PLAN_REVISED, {
        "previous_version": 1,
        "new_version": 2,
        "reason": "Adjust verification.",
    }))
    renderer(_event(EventType.SKILL_LOADED, {"skill": "verify-python-fix"}))
    renderer(_event(EventType.RECOVERY_EXHAUSTED, {
        "category": "permission_denied",
        "reason": "Budget exhausted.",
    }))

    output = capsys.readouterr().out
    assert "Plan v1: Fix regression" in output
    assert "Finish rejected [FINAL_STATE_UNVERIFIED]" in output
    assert "Recovery [completion_rejected] rerun_test (2/4)" in output
    assert "Plan revised v1→v2: Adjust verification." in output
    assert "Skill loaded: verify-python-fix" in output
    assert "Recovery exhausted [permission_denied]: Budget exhausted." in output
