"""Prepare and rehearse a frozen A/B plan, or run an explicitly authorized pilot."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
import zipfile
from contextlib import nullcontext
from pathlib import Path


PILOT = frozenset({
    "encode__httpx-3367", "encode__httpx-3412",
    "pallets__click-3058", "pallets__click-3126",
})
REVISION = "abade24929575ad10549108f51c681c96b32a667"
TOTAL = 600_000


def ab_config(group: str) -> dict:
    if group not in {"A", "B"}:
        raise ValueError("unknown experiment group")
    full = group == "B"
    return {
        "llm": {"provider": "openai", "protocol": "responses", "model": "gpt-5.6-luna",
                "base_url": "${FORGE_EVAL_BASE_URL}", "api_key": "${FORGE_EVAL_API_KEY}",
                "max_output_tokens": 8192, "strict_tool_schema": "auto"},
        "agent": {"run_profile": "interactive", "max_steps": 45, "finish_policy": "balanced",
                  "context_budget_cap": 64_000,
                  "planning_mode": "auto" if full else "off",
                  "recovery_mode": "structured" if full else "off", "skills_enabled": False,
                  "multi_agent_enabled": full, "max_total_tokens": 480_000 if full else TOTAL,
                  "max_workers": 3, "max_delegate_tasks": 3, "total_worker_tokens": 108_000,
                  "worker_timeout_seconds": 120, "log_dir": "/logs/agent/forge"},
        "context": {"repo_map_mode": "incremental" if full else "none",
                    "compaction_mode": "traceable" if full else "off"},
        "memory": {"enabled": full, "auto_capture": full, "defer_promotion": full,
                   **({"root": "/tmp/forge-core20-memory"} if full else {})},
        "mcp": {"enabled": False, "servers": []},
        "tools": {"test": {"python_executable": "python"}},
    }


def build_plan(tasks: list[str], *, pilot: bool = False) -> dict:
    if (
        len(tasks) != 20 or len(set(tasks)) != 20
        or any(not re.fullmatch(r"[a-z0-9-]+__[a-z0-9-]+-\d+", task) for task in tasks)
        or len({task.rsplit("-", 1)[0] for task in tasks}) != 7
        or not PILOT <= set(tasks)
    ):
        raise ValueError("Expected the Core-20 selection (20 unique tasks, 7 repositories)")
    selected = [task for task in tasks if not pilot or task in PILOT]
    return {
        "dataset_revision": REVISION,
        "mode": "pilot" if pilot else "core20",
        "model": "gpt-5.6-luna",
        "protocol": "responses",
        "max_lead_steps": 45,
        "candidate_configs": {group: ab_config(group) for group in "AB"},
        "paid_execution_ready": False,
        "token_budget": {
            "A": {"lead": TOTAL, "worker": 0, "compaction": 0},
            "B": {"lead": 480_000, "worker": 108_000, "compaction": 12_000},
        },
        "trials": [
            {"task": task, "repo": task.rsplit("-", 1)[0], "group": group,
             "trial_id": f"{task}.{group}"}
            for i, task in enumerate(selected)
            for group in ("AB" if i % 2 == 0 else "BA")
        ],
    }


def control_passed(result: dict, details: dict, agent: str) -> bool:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    f2p, passed = details.get("f2p_total"), details.get("f2p_passed")
    p2p, stable = details.get("p2p_total"), details.get("p2p_passed")
    if (result.get("exception_info") is not None or details.get("parse_status") != "ok"
            or any(type(value) is not int for value in (f2p, passed, p2p, stable))
            or f2p <= 0 or not 0 <= passed <= f2p or p2p < 0 or stable != p2p
            or details.get("untracked_failed_count") != 0
            or details.get("reward") != rewards.get("reward")):
        return False
    if agent == "oracle":
        return (rewards.get("reward") == 1 and passed == f2p
                and details.get("exit_code") == 0 and details.get("command_resolved") is True)
    return (agent == "nop" and rewards.get("reward") == 0
            and passed < f2p and details.get("exit_code") == 1)


def run_controls(plan: dict, data: Path, campaign: Path, harbor: Path,
                 *, public_images: bool = False) -> None:
    tasks = list(dict.fromkeys(trial["task"] for trial in plan["trials"]))
    for task in tasks:
        if not (data / task / "task.toml").is_file():
            raise FileNotFoundError(f"missing task: {task}")
    campaign.mkdir(parents=True, exist_ok=False)
    (campaign / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    env = os.environ.copy()
    if public_images:
        docker_config = campaign / "docker-config"
        docker_config.mkdir(mode=0o700)
        env["DOCKER_CONFIG"] = str(docker_config)
    total = 2 * len(tasks)
    with (campaign / "events.jsonl").open("x", encoding="utf-8") as events:
        def report(event, done, task, agent, **data):
            print(f"[{done}/{total}] {task} {agent}: {event}", flush=True)
            events.write(json.dumps({"event": event, "done": done, "total": total,
                                     "task": task, "agent": agent, **data}) + "\n")
            events.flush()

        for i, (task, agent) in enumerate((task, agent) for task in tasks for agent in ("nop", "oracle")):
            name = f"{task}.{agent}"
            report("started", i, task, agent)
            with (campaign / f"{name}.log").open("x", encoding="utf-8") as log:
                command = [str(harbor), "run", "--path", str(data / task), "-a", agent,
                           "--job-name", name, "--jobs-dir", str(campaign), "--n-concurrent", "1",
                           "--n-attempts", "1", "--max-retries", "0"]
                started = time.monotonic()
                with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env) as proc:
                    try:
                        while True:
                            try:
                                status = proc.wait(timeout=15)
                                break
                            except subprocess.TimeoutExpired:
                                report("running", i, task, agent, elapsed_sec=round(time.monotonic() - started))
                    except BaseException:
                        proc.terminate()
                        raise
            trials = list((campaign / name).glob("*/result.json"))
            try:
                if status or len(trials) != 1:
                    raise ValueError("expected exactly one completed Harbor trial")
                outcome = json.loads(trials[0].read_text(encoding="utf-8"))
                if outcome.get("exception_info") is not None:
                    raise ValueError(f"Harbor trial raised an exception; see {name}.log")
                details = json.loads((trials[0].parent / "verifier" / "reward-details.json")
                                     .read_text(encoding="utf-8"))
                if not control_passed(outcome, details, agent):
                    raise ValueError("verifier control assertions did not pass")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                report("failed", i + 1, task, agent, error=str(exc))
                raise RuntimeError(f"Control failed; see {campaign / name}") from exc
            report("passed", i + 1, task, agent)


def rehearse(plan: dict, data: Path, campaign: Path, harbor: Path, wheelhouse: Path,
             task_paths: dict[str, Path], *, resume: bool = False, relay: bool = False,
             paid: bool = False) -> None:
    from examples.harbor.campaign import run_campaign
    from examples.harbor.host_responses import (
        CONNECT_RETRY_WAIT_SECONDS, OUTPUT_ONLY, RATE_LIMIT_WAIT_SECONDS, TOTAL_TOKENS,
        create_host_client, select_budget_contract, trial_key_fd,
    )
    from examples.harbor.snapshot_verifier import harbor_snapshot_verifier
    from examples.harbor.verifier_provenance import TASK_PACKAGES, instrument_task, provenance_matches
    from examples.harbor.verified_snapshot import publish_captured_trial

    root = Path(__file__).resolve().parents[1]
    sources = {trial["task"]: task_paths.get(trial["task"], data / trial["task"]).resolve()
               for trial in plan["trials"]}
    if not all((path / "task.toml").is_file() for path in sources.values()):
        raise ValueError("rehearsal requires all prepared task directories")
    wheel = wheelhouse / "coding_agent-0.1.0-py3-none-any.whl"
    if not wheel.is_file():
        raise ValueError("rehearsal needs a freshly built Forge wheelhouse")
    with zipfile.ZipFile(wheel) as contents:
        if "evals/harbor_offline.py" not in contents.namelist():
            raise ValueError("Forge wheel predates the offline rehearsal entrypoint")
        if relay and not {"evals/harbor_relay.py", "evals/harbor_snapshot_capture.py"} <= set(contents.namelist()):
            raise ValueError("Forge wheel predates the relay or Memory capture entrypoint")
    def file_hash(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    task_files = {}
    for task, path in sources.items():
        task_files[task] = {}
        for child in sorted(path.rglob("*")):
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise ValueError(f"unsupported snapshot entry in {task}")
            if child.is_file():
                task_files[task][child.relative_to(path).as_posix()] = file_hash(child)
    if paid and (not relay or not plan["trials"] or any(t["task"] not in PILOT for t in plan["trials"])):
        raise ValueError("paid qualification is limited to the frozen pilot and requires the host relay")
    mode = "provider-relay" if paid else "offline-relay" if relay else "offline-rehearsal"
    plan = {
        **plan, "execution": mode, "task_paths": {k: str(v) for k, v in sources.items()},
        "task_files_sha256": task_files, "wheelhouse": str(wheelhouse),
        "wheels_sha256": {path.name: file_hash(path) for path in sorted(wheelhouse.glob("*.whl"))},
    }
    if relay:
        plan["verifier_provenance"] = {
            trial["task"]: TASK_PACKAGES[trial["repo"]]
            for trial in plan["trials"] if trial["repo"] in TASK_PACKAGES
        }
        plan["verifier_provenance_sha256"] = file_hash(root / "examples/harbor/verifier_provenance.py")
    # Host Docker transport may need the WSL proxy; the adapter does not pass
    # any of these values to the offline Forge process in the task container.
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    )
           if key in os.environ}
    env["PYTHONPATH"] = str(root)
    env["DOCKER_CONFIG"] = str(campaign / "docker-config")
    secret = ""
    prepare = None
    if paid:
        secret = os.environ.get("FORGE_EVAL_API_KEY", "")
        for key in ("FORGE_EVAL_BASE_URL", "FORGE_EVAL_VERIFIED_COUNT_URL"):
            if key in os.environ:
                env[key] = os.environ[key]
        url = env.get("FORGE_EVAL_BASE_URL", "")
        plan["provider_base_url"] = url

        def prepare():
            verified = bool(url) and env.get("FORGE_EVAL_VERIFIED_COUNT_URL") == url
            with create_host_client(base_url=url, api_key=secret) as client:
                if resume:
                    contract = json.loads((campaign / "plan.json").read_text(encoding="utf-8")).get("budget_contract")
                else:
                    contract = select_budget_contract(client.with_options(timeout=30), plan["model"],
                                                      count_verified=verified)
            selected = contract.get("mode") if isinstance(contract, dict) else None
            if selected not in {TOTAL_TOKENS, OUTPUT_ONLY} or (selected == TOTAL_TOKENS and not verified):
                raise ValueError("missing or unverified frozen budget contract; no trials started")
            plan["budget_contract"] = contract
            plan["token_budget_basis"] = "output" if selected == OUTPUT_ONLY else "input_plus_output"
            plan["max_request_attempts_per_trial"] = 128
            plan["max_output_tokens_per_request"] = 8192
            plan["generation_429_retry"] = {"max_retries": 1, "max_wait_seconds": RATE_LIMIT_WAIT_SECONDS}
            plan["generation_connect_retry"] = {"max_retries": 1, "wait_seconds": CONNECT_RETRY_WAIT_SECONDS}
            env["FORGE_EVAL_BUDGET_CONTRACT"] = selected
            print(f"Budget contract: {selected} ({contract['probe']})", flush=True)

    def driver(trial):
        name, group = trial["trial_id"], trial["group"]
        memory_target = None
        previous_memory = None
        config = campaign / f"{name}.json"
        config.write_text(json.dumps(ab_config(group)), encoding="utf-8")
        (campaign / "docker-config").mkdir(mode=0o700, exist_ok=True)
        frozen = sources[trial["task"]]
        package = plan.get("verifier_provenance", {}).get(trial["task"])
        task_path = frozen
        if package:
            task_path = instrument_task(frozen, campaign / "verifier-inputs" / name / frozen.name, package)
        command = [
            str(harbor), "run", "--path", str(task_path), "--agent",
            "examples.harbor.forge_harbor_agent:ForgeHarborAgent",
            "--ak", f"{'provider_group' if paid else 'offline_group'}={group}",
            "--ak", f"config_path={config}",
            "--ak", f"wheelhouse_path={wheelhouse}", "--job-name", name, "--jobs-dir",
            str(campaign), "--n-concurrent", "1", "--n-attempts", "1", "--max-retries", "0",
        ]
        if package:
            command += ["--ak", f"provenance_package={package}"]
        if relay:
            if not paid:
                command += ["--ak", "offline_relay=true"]
            if group == "B":
                command += ["--ak", "capture_memory=true"]
                memory_dir = campaign / "memory" / trial["repo"]
                memory_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                memory_target = memory_dir / f"{name}.json"
                for earlier in plan["trials"]:
                    if earlier["trial_id"] == name:
                        break
                    if earlier["repo"] == trial["repo"] and earlier["group"] == "B":
                        generation = memory_dir / f"{earlier['trial_id']}.json"
                        if generation.is_file():
                            previous_memory = generation
                if previous_memory is not None:
                    command += ["--ak", f"memory_bundle_path={previous_memory}",
                                "--ak", f"memory_repo_id={trial['repo']}"]
        started = time.monotonic()
        with (campaign / f"{name}.log").open("x", encoding="utf-8") as log, (
            trial_key_fd(secret) if paid else nullcontext()
        ) as fd:
            child_env = {**env, "FORGE_EVAL_KEY_FD": str(fd)} if fd is not None else env
            with subprocess.Popen(command, cwd=campaign, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                  pass_fds=(fd,) if fd is not None else ()) as proc:
                try:
                    while True:
                        try:
                            status = proc.wait(timeout=15)
                            break
                        except subprocess.TimeoutExpired:
                            print(f"{name}: running ({time.monotonic() - started:.0f}s)", flush=True)
                except BaseException:
                    proc.terminate()
                    raise
        results = list((campaign / name).glob("*/result.json"))
        if status or len(results) != 1:
            return {"status": "infrastructure", "reason": "missing Harbor trial", "log": f"{name}.log"}
        result = json.loads(results[0].read_text(encoding="utf-8"))
        metadata = (result.get("agent_result") or {}).get("metadata") or {}
        usage = metadata.get("forge_usage") or {}
        detail_path = results[0].parent / "verifier" / "reward-details.json"
        detail = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.is_file() else {}
        harbor_reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        if (result.get("exception_info") is not None or metadata.get("mode") != mode
                or not usage.get("finished_calls") or detail.get("parse_status") != "ok"
                or detail.get("reward") not in (0, 1) or detail["reward"] != harbor_reward):
            return {"status": "infrastructure", "reason": "trial output not verified", "log": f"{name}.log"}
        if paid and metadata.get("budget_contract") != plan["budget_contract"]["mode"]:
            return {"status": "infrastructure", "reason": "trial budget contract mismatch", "log": f"{name}.log"}
        if package:
            receipt_path = results[0].parent / "verifier" / "forge-source-provenance.json"
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                with (frozen / "task.toml").open("rb") as stream:
                    base_commit = tomllib.load(stream)["metadata"]["repo2env"]["pr_runtime"]["base_commit"]
                verified = provenance_matches(metadata.get("source_provenance"), receipt, package=package,
                                               base_commit=base_commit, exit_code=detail.get("exit_code"))
            except (OSError, ValueError, KeyError, TypeError):
                verified = False
            if not verified:
                return {"status": "infrastructure", "reason": "verifier source provenance missing or mismatched",
                        "log": f"{name}.log", "result": str(results[0])}
        if relay and group == "B" and metadata.get("memory_capture_status") != "captured":
            return {"status": "infrastructure", "reason": "missing Memory capture", "log": f"{name}.log"}
        promoted = 0
        if memory_target is not None and detail["reward"] == 1:
            frozen = sources[trial["task"]]
            with (frozen / "task.toml").open("rb") as stream:
                commit = tomllib.load(stream)["metadata"]["repo2env"]["pr_runtime"]["base_commit"]
            verify = harbor_snapshot_verifier(
                frozen_task=frozen, evidence_dir=campaign / "snapshot-verifier" / name, harbor=harbor,
            )
            promoted = publish_captured_trial(
                result_file=results[0], target=memory_target, repo_id=trial["repo"], expected_base=commit,
                previous=previous_memory.read_bytes() if previous_memory is not None else None, verify=verify,
            )
        return {"status": "failed" if paid and detail["reward"] != 1 else "passed",
                "verifier_reward": detail["reward"],
                "provider": "provider-relay" if paid else "offline-double",
                "input_usage": metadata.get("host_input_usage"),
                "memory_promoted": promoted, "result": str(results[0])}

    run_campaign(campaign, plan, driver, resume=resume, prepare=prepare)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "controls", "rehearse", "qualify", "probe-count"])
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--task", help="Run just this task from the selected plan")
    parser.add_argument("--data", type=Path)
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--harbor", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--task-path", action="append", default=[], metavar="TASK=PATH")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--relay", action="store_true", help="Use the credential-free Docker stdio relay")
    parser.add_argument("--execute-paid", action="store_true", help="Explicitly authorize provider charges")
    parser.add_argument("--public-images", action="store_true",
                        help="Use a clean campaign-local Docker config for public images")
    args = parser.parse_args()
    if args.action == "qualify" and not (args.pilot and args.execute_paid):
        parser.error("qualify requires --pilot and --execute-paid")
    if args.action == "probe-count":
        if not args.execute_paid:
            parser.error("probe-count requires --execute-paid; the provider may charge for counting")
        from examples.harbor.host_responses import create_host_client, probe_input_count

        try:
            with create_host_client(base_url=os.environ.get("FORGE_EVAL_BASE_URL", ""),
                                    api_key=os.environ.get("FORGE_EVAL_API_KEY", "")) as client:
                count = probe_input_count(client, "gpt-5.6-luna")
        except Exception as exc:
            parser.exit(1, f"input count probe failed ({type(exc).__name__}); no generation attempted\n")
        print(f"input count returned {count} tokens; billing/accounting upper bound is not proven")
        return
    if args.action != "qualify" and args.execute_paid:
        parser.error("--execute-paid is only valid for qualify or probe-count")
    if args.selection is None:
        parser.error("this action requires --selection")
    tasks = args.selection.read_text(encoding="utf-8").splitlines()
    plan = build_plan(tasks, pilot=args.pilot)
    if args.task is not None:
        plan["trials"] = [trial for trial in plan["trials"] if trial["task"] == args.task]
        if not plan["trials"]:
            parser.error("--task must belong to the selected plan")
    if args.action == "plan":
        print(json.dumps(plan, indent=2))
        return
    if not all((args.data, args.campaign, args.harbor)):
        parser.error("execution requires --data, --campaign, and --harbor")
    if args.action in {"rehearse", "qualify"}:
        if args.wheelhouse is None:
            parser.error("rehearse/qualify requires --wheelhouse")
        paths = {}
        for value in args.task_path:
            task, separator, path = value.partition("=")
            if not separator or not path or task in paths or task not in {t["task"] for t in plan["trials"]}:
                parser.error("--task-path requires one selected TASK=PATH mapping per override")
            paths[task] = Path(path)
        rehearse(plan, args.data.resolve(), args.campaign.resolve(), args.harbor.absolute(),
                 args.wheelhouse.resolve(), paths, resume=args.resume,
                 relay=args.relay or args.execute_paid, paid=args.execute_paid)
        return
    run_controls(plan, args.data.resolve(), args.campaign.resolve(), args.harbor.absolute(),
                 public_images=args.public_images)


if __name__ == "__main__":
    main()
