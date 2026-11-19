"""Sequential, durable campaign state for prepared trial drivers."""
import json
import os
from collections.abc import Callable
from pathlib import Path


def _atomic_json(path: Path, value: dict) -> None:
    scratch = path.with_suffix(".new")
    with scratch.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    scratch.replace(path)


def run_campaign(root: Path, plan: dict, run_trial: Callable[[dict], dict], *,
                 resume: bool = False, prepare: Callable[[], None] | None = None) -> None:
    """Resume completed trials only; a possibly started trial is not replayable."""
    root.mkdir(parents=True, exist_ok=resume)
    # Exclusive even before reading plan/state. A left-behind lock is ambiguous.
    lock = root / "dispatch.lock"
    with lock.open("x", encoding="utf-8"):
        pass
    try:
        if prepare is not None:
            prepare()
        _dispatch(root, plan, run_trial, resume=resume)
    finally:
        lock.unlink()


def _dispatch(root, plan, run_trial, *, resume):
    frozen = root / "plan.json"
    state_path = root / "progress.json"
    trials = plan["trials"]
    ids = [trial["trial_id"] for trial in trials]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("campaign requires unique trial IDs")
    if resume:
        if json.loads(frozen.read_text(encoding="utf-8")) != plan:
            raise ValueError("frozen campaign plan mismatch")
        progress = json.loads(state_path.read_text(encoding="utf-8"))
        if progress["active"] is not None or progress.get("blocked"):
            raise RuntimeError("campaign needs manual reconciliation; no trial was replayed")
        completed = progress["completed"]
        if list(completed) != sorted(ids[:len(completed)]):
            raise ValueError("campaign completion set is not a prefix of the plan")
    else:
        _atomic_json(frozen, plan)
        completed = {}
        progress = {"active": None, "completed": completed, "total": len(ids), "blocked": False}
        _atomic_json(state_path, progress)

    with (root / "events.jsonl").open("a", encoding="utf-8") as events:
        def record(event, trial_id):
            events.write(json.dumps({"event": event, "trial": trial_id, "done": len(completed),
                                     "total": len(ids)}) + "\n")
            events.flush()
            print(f"[{len(completed)}/{len(ids)}] {trial_id}: {event}", flush=True)

        for trial in trials:
            trial_id = trial["trial_id"]
            if trial_id in completed:
                continue
            progress["active"] = trial_id
            _atomic_json(state_path, progress)
            record("started", trial_id)
            outcome = run_trial(trial)
            if outcome.get("status") not in {"passed", "failed", "infrastructure"}:
                raise ValueError("trial did not return a classified outcome")
            completed[trial_id] = outcome
            progress["active"] = None
            progress["blocked"] = outcome["status"] == "infrastructure"
            _atomic_json(state_path, progress)
            record(outcome["status"], trial_id)
            if progress["blocked"]:
                raise RuntimeError(f"campaign stopped on infrastructure failure: {trial_id}")
