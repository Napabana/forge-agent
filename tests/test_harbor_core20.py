import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.schema import _parse
from evals.harbor_snapshot_capture import capture_candidate
from examples.harbor.forge_usage_extract import extract, summarize
from examples.harbor.campaign import run_campaign
from examples.harbor.docker_stdio import run_with_broker
from examples.harbor.host_responses import HostResponses, bind_host_client, create_host_client, serve_trial_broker
from examples.harbor.memory_publication import promote_candidate, publish_verified_trial
from examples.harbor.snapshot_verifier import harbor_snapshot_verifier
from examples.harbor.verified_snapshot import grade_and_publish, publish_captured_trial, seal_snapshot
from llm.request_budget import RequestBudget
from project_memory.runtime import MemoryRuntime
from project_memory.transfer import export_verified_memory, import_verified_memory
from scripts.run_harbor_core20 import PILOT, ab_config, build_plan, control_passed


def event(kind, **payload):
    return {"event_type": kind, "payload": payload}


def test_usage_is_disjoint_and_partial(tmp_path):
    a = dict(input_tokens=8, output_tokens=2, total_tokens=10)
    lead = [
        event("llm_call_finished", provider_usage=a),
        event("workers_summary", worker_usage=a, usage_complete=True, worker_count=1),
        event("context_compaction_failed", summary_usage=a),
        event("context_compacted", summary_usage=a, semantic_error="failed"),
        event("run_terminated", status="success"),
    ]
    (tmp_path / "lead.jsonl").write_text("\n".join(map(json.dumps, lead)), encoding="utf-8")
    (tmp_path / "worker.jsonl").write_text(
        json.dumps(event("task_start", entrypoint="worker")), encoding="utf-8")
    result = extract(tmp_path)
    assert result["trace"] == "lead.jsonl"
    assert result["total_tokens"] is None
    assert result["by_component"]["workers"]["total_tokens"] == 10
    assert result["observed_by_component"]["semantic"]["total_tokens"] == 10
    assert result["worker_metrics"]["worker_count"] == 1
    complete = [*lead[:2], event("context_compacted", summary_usage=a), lead[-1]]
    assert summarize(complete)["total_tokens"] == 30
    assert summarize(complete)["reasoning_tokens"] is None


def test_usage_matches_dashboard_without_counting_cache_twice():
    from llm.openai_responses import _response_usage
    from llm.usage import SessionUsage

    def usage(input_tokens, output_tokens, cache):
        details = {} if cache is None else {"cached_tokens": cache}
        return _response_usage({"usage": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                                          "input_tokens_details": details}}, [], "")

    worker = SessionUsage()
    worker.record(usage(3707, 21, 0))
    rows = [event("llm_call_finished", provider_usage=usage(3574, 9, 3328).to_dict()),
            event("workers_summary", worker_usage=worker.to_dict(), usage_complete=True),
            event("context_compacted", summary_usage=usage(3749, 34, 3456).to_dict()),
            event("run_terminated")]
    result = summarize(rows)
    assert [result[key] for key in ("input_tokens", "cached_tokens", "uncached_input_tokens",
                                    "output_tokens", "total_tokens", "cost_usd")] == [
        11030, 6784, 4246, 64, 11094, None,
    ]
    assert result["by_component"]["workers"]["cached_tokens"] == 0
    worker.record(usage(10, 1, None))
    rows[1]["payload"]["worker_usage"] = worker.snapshot().to_dict()
    unknown = summarize(rows)
    assert unknown["input_tokens"] == 11040
    assert unknown["cached_tokens"] is unknown["uncached_input_tokens"] is None
    assert unknown["observed_by_component"]["lead"]["uncached_input_tokens"] == 246


def test_plan_pilot_and_full_configs():
    tasks = [*sorted(PILOT), "aa__bb-1", "aa__cc-1", "aa__dd-1", "aa__ee-1",
             *(f"aa__ff-{n}" for n in range(12))]
    full, pilot = build_plan(tasks), build_plan(tasks, pilot=True)
    assert len({trial["trial_id"] for trial in full["trials"]}) == 40
    assert len(pilot["trials"]) == 8
    assert {trial["task"] for trial in pilot["trials"]} == PILOT
    assert [trial["group"] for trial in pilot["trials"][:4]] == list("ABBA")
    assert not full["paid_execution_ready"]
    A, B = (_parse(ab_config(group)) for group in "AB")
    assert A.agent.max_steps == B.agent.max_steps == 45
    assert A.llm.protocol == B.llm.protocol == "responses"
    assert not A.memory.enabled and not A.agent.multi_agent_enabled
    assert B.memory.auto_capture and B.memory.defer_promotion and B.agent.multi_agent_enabled
    assert sum(full["token_budget"]["A"].values()) == sum(full["token_budget"]["B"].values())


def test_deferred_memory_never_journals_unverified_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "example.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    memory = MemoryRuntime(str(repo), root=str(tmp_path / "store"), defer_promotion=True)
    task = SimpleNamespace(task_id="fake-run")
    candidate = memory.writer.propose(kind="experience", title="example",
                                      content="Use a focused test", related_files=["example.py"],
                                      tags=[], run_id=task.task_id, repo=str(repo))
    memory.conclude(None, task=task, acceptance="passed", trace_path=None)
    memory.record_exception(task, RuntimeError("no"), trace_path=None)
    assert memory.writer.pending()[0]["candidate_id"] == candidate
    assert not memory.store.read_activity()
    assert not memory.store.read_records()


def test_controls_distinguish_test_failure_from_infrastructure():
    details = dict(reward=0, parse_status="ok", f2p_total=1, f2p_passed=0,
                   p2p_total=2, p2p_passed=2, untracked_failed_count=0, exit_code=1)
    result = {"verifier_result": {"rewards": {"reward": 0}}}
    assert control_passed(result, details, "nop")
    assert not control_passed(result, {**details, "exit_code": 4}, "nop")
    assert not control_passed({**result, "exception_info": {}}, details, "nop")
    assert not control_passed(result, {**details, "parse_status": "fallback"}, "nop")
    result["verifier_result"]["rewards"]["reward"] = 1
    assert control_passed(result, {**details, "reward": 1, "f2p_passed": 1, "exit_code": 0,
                                    "command_resolved": True}, "oracle")


def test_budget_reserves_concurrent_workers_and_charges_unknown_calls():
    budget = RequestBudget(total=50, pools={"lead": 21, "workers": 24, "compaction": 5})

    def reserve():
        try:
            return budget.reserve("workers", input_upper=10, max_output=10)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        workers = [row for row in pool.map(lambda _: reserve(), range(2)) if row]
    assert len(workers) == 1
    budget.settle(workers[0], input_tokens=None, output_tokens=None)
    assert reserve() is None
    side = budget.reserve("compaction", input_upper=3, max_output=5)
    assert side.output_limit == 2
    lead = budget.reserve("lead", input_upper=10, max_output=10)
    budget.settle(lead, input_tokens=8, output_tokens=2)
    retry = budget.reserve("lead", input_upper=10, max_output=10)
    assert retry.output_limit == 1
    with pytest.raises(RuntimeError, match="Provider usage"):
        budget.settle(retry, input_tokens=11, output_tokens=1)
    with pytest.raises(RuntimeError, match="invalidated"):
        budget.reserve("lead", input_upper=0, max_output=1)


def test_publish_from_matching_verified_snapshot_only(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "example.py"
    source.write_text("answer = 1\n", encoding="utf-8")
    staged = MemoryRuntime(str(repo), root=str(tmp_path / "pending"), defer_promotion=True)
    publish = MemoryRuntime(str(repo), root=str(tmp_path / "generation"), defer_promotion=True)
    staged.writer.propose(kind="feature", title="answer", content="Verify default values",
                          related_files=["example.py"], tags=[], run_id="run", repo=str(repo),
                          commit="base")
    result, details = tmp_path / "result.json", tmp_path / "details.json"
    result.write_text(json.dumps({"verifier_result": {"rewards": {"reward": 1}}}), encoding="utf-8")
    details.write_text(json.dumps({"reward": 1, "parse_status": "ok"}), encoding="utf-8")
    kwargs = dict(writer=publish.writer, proposal=staged.writer.pending()[0], verified_repo=repo,
                  run_id="run", base_commit="base", snapshot_sha256="a" * 64,
                  verifier_snapshot_sha256="a" * 64, changed_files=frozenset({"example.py"}),
                  harbor_result=result, verifier_details=details, run_usage={"run_status": "success"})
    assert promote_candidate(**{**kwargs, "verifier_snapshot_sha256": "b" * 64}) is None
    assert promote_candidate(**{**kwargs, "run_id": "another"}) is None
    assert not publish.store.read_records()
    candidate = promote_candidate(**kwargs)
    assert candidate == promote_candidate(**kwargs)
    assert publish.store.read_records()[0]["confidence"] == "verified"

    publish.writer.append_activity({"summary": "unverified trial journal"})
    publish.writer.propose(kind="feature", title="Unpublished", content="Do not import",
                           related_files=["example.py"], tags=[], run_id="later", repo=str(repo))
    payload = export_verified_memory(publish.store, "org/repo")
    assert b"Unpublished" not in payload and b"unverified trial journal" not in payload
    next_repo = tmp_path / "isolated-repo"
    next_repo.mkdir()
    (next_repo / "example.py").write_bytes(source.read_bytes())
    next_trial = MemoryRuntime(str(next_repo), root=str(tmp_path / "isolated-store"))
    with pytest.raises(ValueError, match="repository or version mismatch"):
        import_verified_memory(next_trial.store, "other/repo", payload)
    corrupted = json.loads(payload)
    corrupted["records"][0]["source_files"] = ["../outside"]
    with pytest.raises(ValueError, match="source-file"):
        import_verified_memory(next_trial.store, "org/repo", json.dumps(corrupted).encode())
    assert not next_trial.store.read_records()
    assert import_verified_memory(next_trial.store, "org/repo", payload) == 1
    assert next_trial.retrieval.search("default")[0]["memory_id"] == candidate
    assert not next_trial.writer.pending() and not next_trial.store.read_activity()
    with pytest.raises(ValueError, match="empty trial store"):
        import_verified_memory(next_trial.store, "org/repo", payload)

    publication = {k: v for k, v in kwargs.items() if k not in ("writer", "proposal")}
    output = tmp_path / "verified.json"
    assert publish_verified_trial(target=output, previous=None, repo_id="org/repo",
                                  proposals=[kwargs["proposal"]], **publication) == 1
    assert json.loads(output.read_bytes())["records"][0]["memory_id"] == candidate
    with pytest.raises(FileExistsError):
        publish_verified_trial(target=output, previous=None, repo_id="org/repo",
                               proposals=[kwargs["proposal"]], **publication)
    rejected = tmp_path / "rejected.json"
    assert publish_verified_trial(
        target=rejected, previous=output.read_bytes(), repo_id="org/repo", proposals=[kwargs["proposal"]],
        **{**publication, "snapshot_sha256": "b" * 64},
    ) == 0
    assert not rejected.exists()

    source.write_text("answer = 2\n", encoding="utf-8")
    assert promote_candidate(**kwargs) is None


def test_host_owns_destination_and_does_not_forward_disallowed_requests():
    sent = []
    counts = []

    def send(body):
        sent.append(body)
        return {"usage": {"input_tokens": 3, "output_tokens": min(2, body["max_output_tokens"])}}

    def count(body):
        counts.append(body)
        return 3

    broker = HostResponses(model="frozen", budget=RequestBudget(total=12, pools={
        "lead": 8, "worker": 4, "compaction": 0,
    }), count_input=count, send=send, max_output=6, max_requests=2)
    body = {"model": "frozen", "input": [{"role": "user", "content": "mock"}], "max_output_tokens": 20}
    for invalid in (dict(body, url="https://wrong.invalid"), dict(body, model="wrong"),
                    dict(body, tools=[{"type": "web_search"}]),
                    dict(body, input=[{"type": "input_file", "file_id": "unknown"}])):
        with pytest.raises(ValueError):
            broker.respond("lead", invalid)
    broker.respond("lead", body)
    assert sent[0]["max_output_tokens"] == 5
    broker.respond("worker", body)
    assert sent[1]["max_output_tokens"] == 1
    with pytest.raises(RuntimeError, match="attempt cap"):
        broker.respond("lead", body)
    assert len(counts) == 2


def test_eight_mock_trials_complete_once_and_ambiguous_dispatch_is_not_replayed(tmp_path):
    plan = {"trials": [{"trial_id": f"trial-{i}"} for i in range(8)]}
    ran = []

    def driver(trial):
        ran.append(trial["trial_id"])
        return {"status": "passed", "provider": "fake"}

    good = tmp_path / "good"
    run_campaign(good, plan, driver)
    run_campaign(good, plan, driver, resume=True)
    assert len(ran) == 8
    assert len(json.loads((good / "progress.json").read_text())["completed"]) == 8
    broken = tmp_path / "broken"

    def interrupted(trial):
        raise RuntimeError("ambiguous")

    with pytest.raises(RuntimeError, match="ambiguous"):
        run_campaign(broken, plan, interrupted)
    with pytest.raises(RuntimeError, match="reconciliation"):
        run_campaign(broken, plan, driver, resume=True)
    assert len(ran) == 8

    infrastructure = tmp_path / "infrastructure"
    with pytest.raises(RuntimeError, match="stopped on infrastructure"):
        run_campaign(infrastructure, plan, lambda trial: {"status": "infrastructure"})
    assert list(json.loads((infrastructure / "progress.json").read_text())["completed"]) == ["trial-0"]


def test_offline_backend_has_no_sdk_client(monkeypatch):
    from agent.task import ActionType
    from evals.harbor_offline import input_count, offline_backend

    def forbid(*args, **kwargs):
        raise AssertionError("offline rehearsal must not instantiate an SDK client")

    monkeypatch.setattr("openai.OpenAI", forbid)
    config = _parse({"llm": {"max_output_tokens": 60}})
    backend = offline_backend({"model": "frozen", "max_tokens": config.llm.max_tokens})
    budget = RequestBudget(total=100, pools={"lead": 100})
    backend.bind_request_budget(budget, "lead", input_count)
    response = backend.complete([], [])
    assert response.usage.output_tokens == 1
    assert response.action.action_type is ActionType.GIVE_UP


def test_provider_requires_explicit_endpoint_confirmation(monkeypatch):
    from examples.harbor import host_responses

    env = {"FORGE_EVAL_BASE_URL": "https://expected.invalid/v1", "FORGE_EVAL_API_KEY": "fake-key"}
    received = []
    monkeypatch.setattr(host_responses, "create_host_client", lambda **options: received.append(options))
    with pytest.raises(ValueError, match="verified"):
        host_responses.create_confirmed_client(env)
    env["FORGE_EVAL_VERIFIED_COUNT_URL"] = "https://different.invalid/v1"
    with pytest.raises(ValueError, match="verified"):
        host_responses.create_confirmed_client(env)
    assert not received
    env["FORGE_EVAL_VERIFIED_COUNT_URL"] = env["FORGE_EVAL_BASE_URL"]
    host_responses.create_confirmed_client(env)
    assert received == [{"base_url": env["FORGE_EVAL_BASE_URL"], "api_key": "fake-key"}]


@pytest.mark.parametrize("reply,verified,mode", [
    (b'{"input_tokens":42}', True, "total_tokens"),
    (b'{"input_tokens":42}', False, "output_only"),
    (b'', True, "output_only"),
])
def test_first_count_probe_selects_contract_without_generating(reply, verified, mode):
    import httpx
    from openai import OpenAI
    from examples.harbor.host_responses import select_budget_contract

    paths = []

    def send(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=reply, headers={"Content-Type": "application/json"})

    with OpenAI(api_key="fake", base_url="https://test.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(send))) as client:
        selection = select_budget_contract(client, "frozen", count_verified=verified)
    assert selection["mode"] == mode
    assert paths == ["/v1/responses/input_tokens"]


def test_output_only_limits_wire_attempts_and_output_but_observes_input():
    from llm.request_budget import RequestBudgetExhausted

    sent = []

    def send(body):
        sent.append(body["max_output_tokens"])
        if len(sent) == 1:
            return {"usage": {"input_tokens": 4000, "output_tokens": 3}}
        return {"usage": {}}

    broker = HostResponses(model="frozen", budget=RequestBudget(total=7, pools={"lead": 7}),
                           count_input=None, send=send, max_output=5, max_requests=3)
    body = {"model": "frozen", "input": "hi"}
    assert broker.respond("lead", body)["usage"]["input_tokens"] == 4000
    broker.respond("lead", body)
    with pytest.raises(RequestBudgetExhausted, match="budget exhausted"):
        broker.respond("lead", body)
    with pytest.raises(RequestBudgetExhausted, match="attempt cap"):
        broker.respond("lead", body)
    assert sent == [5, 4]
    assert broker.input_usage() == {"observed_input_tokens": 4000, "unknown_input_calls": 1,
                                    "rate_limit_retries": 0, "connect_retries": 0}


@pytest.mark.parametrize("header,attempts,recover,waits", [
    ("7", 2, True, [7]), (None, 2, True, [60]), ("90", 2, False, []), ("7", 1, False, []),
    ("7", 3, False, [7]),
])
def test_generation_429_retry_is_bounded_and_recharged(monkeypatch, header, attempts, recover, waits):
    import httpx
    from openai import OpenAI, RateLimitError
    from examples.harbor import host_responses

    sent = []
    observed_waits = []
    monkeypatch.setattr(host_responses, "sleep", observed_waits.append)

    def fake_provider(request):
        assert request.url.path == "/v1/responses"
        sent.append(json.loads(request.content)["max_output_tokens"])
        if len(sent) == 1 or not recover:
            return httpx.Response(429, headers={"Retry-After": header} if header else {},
                                  json={"error": {"message": "test limit"}})
        return httpx.Response(200, json={"output": [], "usage": {"input_tokens": 21, "output_tokens": 2}})

    with OpenAI(api_key="fake", base_url="https://test.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(fake_provider))) as client:
        broker = bind_host_client(client, model="frozen", budget=RequestBudget(total=11, pools={"lead": 11}),
                                  max_output=8, max_requests=attempts, contract="output_only", retry_rate_limit=True)
        body = {"model": "frozen", "input": "hi"}
        if recover:
            assert broker.respond("lead", body)["usage"]["input_tokens"] == 21
        else:
            with pytest.raises(RateLimitError):
                broker.respond("lead", body)
    assert observed_waits == waits
    assert sent == ([8, 3] if waits else [8])
    assert broker.input_usage()["rate_limit_retries"] == len(waits)
    assert broker.input_usage()["unknown_input_calls"] == len(sent) - int(recover)


@pytest.mark.parametrize("failure,expected", [
    ("ConnectError", 2), ("ConnectTimeout", 2), ("ReadTimeout", 1), ("ReadError", 1), ("WriteError", 1),
])
def test_generation_retries_only_failure_to_establish_connection(monkeypatch, failure, expected):
    import httpx
    from openai import APIConnectionError, OpenAI
    from examples.harbor import host_responses

    sent, waits = [], []
    monkeypatch.setattr(host_responses, "sleep", waits.append)

    def fake_provider(request):
        sent.append(json.loads(request.content)["max_output_tokens"])
        if len(sent) == 1:
            raise getattr(httpx, failure)("fake transport failure", request=request)
        return httpx.Response(200, json={"output": [], "usage": {"input_tokens": 10, "output_tokens": 2}})

    with OpenAI(api_key="fake", base_url="https://test.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(fake_provider))) as client:
        broker = bind_host_client(client, model="frozen", budget=RequestBudget(total=11, pools={"lead": 11}),
                                  max_output=8, max_requests=2, contract="output_only", retry_connect=True)
        body = {"model": "frozen", "input": "hi"}
        if expected == 2:
            assert broker.respond("lead", body)["usage"]["output_tokens"] == 2
        else:
            with pytest.raises(APIConnectionError):
                broker.respond("lead", body)
    assert len(sent) == expected
    assert sent == ([8, 3] if expected == 2 else [8])
    assert waits == ([5] if expected == 2 else [])
    assert broker.input_usage()["connect_retries"] == expected - 1


@pytest.mark.parametrize("matching_receipt", [True, False])
def test_paid_plan_freezes_fallback_and_requires_verifier_receipt(tmp_path, monkeypatch, matching_receipt):
    from contextlib import nullcontext
    from zipfile import ZipFile
    from examples.harbor import host_responses
    from scripts.run_harbor_core20 import rehearse

    task = sorted(PILOT)[0]
    source = tmp_path / task
    source.mkdir()
    (source / "task.toml").write_text('[metadata.repo2env.pr_runtime]\nbase_commit = "base"\n')
    (source / "tests").mkdir()
    original = '#!/bin/bash\ncd /workspace\npython -m pytest\n'
    (source / "tests/test.sh").write_text(original)
    with ZipFile(tmp_path / "coding_agent-0.1.0-py3-none-any.whl", "w") as wheel:
        for module in ("harbor_offline", "harbor_relay", "harbor_snapshot_capture"):
            wheel.writestr(f"evals/{module}.py", "")
    plan = {"model": "gpt-5.6-luna", "trials": [{"task": task, "group": "A", "trial_id": "test.A",
                                               "repo": "encode__httpx"}]}
    campaign = tmp_path / "campaign"
    probes = []
    launched = []
    monkeypatch.setenv("FORGE_EVAL_BASE_URL", "https://fake.invalid/v1")
    monkeypatch.setenv("FORGE_EVAL_API_KEY", "fake")
    monkeypatch.setattr(host_responses, "create_host_client", lambda **kw: nullcontext(
        SimpleNamespace(with_options=lambda **opts: None)))

    def choose(*args, **kwargs):
        probes.append(1)
        return {"mode": "output_only", "probe": "no_count"}

    def popen(command, *, env, **kwargs):
        launched.append(env["FORGE_EVAL_BUDGET_CONTRACT"])
        assert "FORGE_EVAL_API_KEY" not in env
        result = campaign / "test.A" / "trial"
        staged_task = Path(command[command.index("--path") + 1])
        assert (staged_task / "tests/__forge_original_test.sh").read_text() == original
        assert (source / "tests/test.sh").read_text() == original
        (result / "verifier").mkdir(parents=True)
        (result / "verifier/reward-details.json").write_text('{"parse_status":"ok","reward":0,"exit_code":1}')
        snapshot = {"package": "httpx", "base_commit": "base", "sha256": "a" * 64,
                    "python": "/usr/local/bin/python", "file_count": 1}
        (result / "verifier/forge-source-provenance.json").write_text(json.dumps({
            "before": snapshot, "after": snapshot, "pytest_exit_status": 1,
            "imports": {"httpx": {"in_scope": matching_receipt, "sha256": "b" * 64}},
        }))
        (result / "result.json").write_text(json.dumps({"verifier_result": {"rewards": {"reward": 0}},
                                                        "agent_result": {"metadata": {
            "mode": "provider-relay", "budget_contract": "output_only", "source_provenance": snapshot,
            "forge_usage": {"finished_calls": 1}, "host_input_usage": {"observed_input_tokens": 3574},
        }}}))
        return nullcontext(SimpleNamespace(wait=lambda **opts: 0))

    monkeypatch.setattr(host_responses, "select_budget_contract", choose)
    monkeypatch.setattr(subprocess, "Popen", popen)
    if not matching_receipt:
        with pytest.raises(RuntimeError, match="infrastructure failure"):
            rehearse(plan, tmp_path, campaign, tmp_path / "fake-harbor", tmp_path, {}, relay=True, paid=True)
        result = json.loads((campaign / "progress.json").read_text())["completed"]["test.A"]
        assert result["status"] == "infrastructure" and "provenance" in result["reason"]
        assert "verifier_reward" not in result
        return
    for resume in (False, True):
        rehearse(plan, tmp_path, campaign, tmp_path / "fake-harbor", tmp_path, {},
                 resume=resume, relay=True, paid=True)
    assert probes == [1] and launched == ["output_only"]
    assert json.loads((campaign / "plan.json").read_text())["token_budget_basis"] == "output"
    result = json.loads((campaign / "progress.json").read_text())["completed"]["test.A"]
    assert result["input_usage"]["observed_input_tokens"] == 3574


def test_verifier_plugin_records_actual_import_and_exact_source(tmp_path):
    import sys
    from examples.harbor import verifier_provenance as probe

    repo = tmp_path / "repo"
    package = repo / "src/click"
    package.mkdir(parents=True)
    source = package / "__init__.py"
    source.write_text("VALUE = 42\n")
    (repo / "test_receipt.py").write_text("import click\ndef test_value(): assert click.VALUE == 42\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Offline", "-c", "user.email=offline@example.invalid",
                    "commit", "-qm", "frozen", "--allow-empty"], cwd=repo, check=True)
    expected = probe.source_snapshot(repo, "click")
    receipt_path = tmp_path / "receipt.json"
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": "verifier_provenance",
           "PYTHONPATH": os.pathsep.join(
        (str(repo / "src"), str(Path(probe.__file__).parent))), "FORGE_PROVENANCE_PACKAGE": "click",
        "FORGE_PROVENANCE_RECEIPT": str(receipt_path)}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                            cwd=repo, env=env, check=True, capture_output=True, timeout=30)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["imports"]["click"]["path"] == str(source.resolve())
    assert probe.provenance_matches(expected, receipt, package="click", base_commit=expected["base_commit"],
                                    exit_code=result.returncode)
    source.write_text("VALUE = 99\n")
    assert not probe.provenance_matches(probe.source_snapshot(repo, "click"), receipt, package="click",
                                        base_commit=expected["base_commit"], exit_code=0)


@pytest.mark.parametrize("args", [["qualify", "--pilot"], ["probe-count"], ["rehearse", "--execute-paid"]])
def test_paid_commands_require_explicit_mode_and_opt_in(monkeypatch, args, capsys):
    import sys
    from scripts.run_harbor_core20 import main

    monkeypatch.setattr(sys, "argv", ["run_harbor_core20.py", *args])
    with pytest.raises(SystemExit, match="2"):
        main()
    assert "--execute-paid" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="WSL/Linux credential transport")
def test_trial_credential_reaches_only_the_designated_host_process():
    import subprocess
    import sys
    from examples.harbor.host_responses import trial_key_fd

    root = Path(__file__).resolve().parents[1]
    script = """
import os
from examples.harbor import host_responses as host
assert 'FORGE_EVAL_API_KEY' not in os.environ
host.create_host_client = lambda **opts: opts
assert host.create_trial_client(os.environ)['api_key'] == 'offline-sentinel'
try:
    os.fstat(int(os.environ['FORGE_EVAL_KEY_FD']))
except OSError:
    pass
else:
    raise RuntimeError('host credential descriptor not closed')
"""
    with trial_key_fd("offline-sentinel") as fd:
        assert not os.get_inheritable(fd)
        env = {"PYTHONPATH": str(root), "FORGE_EVAL_KEY_FD": str(fd),
               "FORGE_EVAL_BASE_URL": "https://test.invalid/v1",
               "FORGE_EVAL_VERIFIED_COUNT_URL": "https://test.invalid/v1"}
        subprocess.run([sys.executable, "-c", script], env=env, pass_fds=(fd,), check=True, timeout=15)


def test_brokered_sdk_credential_only_on_host_requests():
    import httpx
    from openai import OpenAI

    calls = []

    def provider(request):
        calls.append(request)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 3})
        return httpx.Response(200, json={"status": "completed", "output": [],
                                        "usage": {"input_tokens": 3, "output_tokens": 2}})

    with pytest.raises(ValueError, match="fixed HTTPS"):
        create_host_client(base_url="https://secret@bad.invalid", api_key="offline")
    with httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=False) as transport:
        with OpenAI(api_key="sentinel-host-key", base_url="https://test.invalid/v1",
                    http_client=transport) as client:
            host = bind_host_client(client, model="frozen", max_output=8, max_requests=1,
                                    budget=RequestBudget(total=10, pools={"lead": 10}))
            result = host.respond("lead", {"model": "frozen", "input": "offline"})
    assert [req.url.path for req in calls] == ["/v1/responses/input_tokens", "/v1/responses"]
    assert all(req.headers.get("Authorization") == "Bearer sentinel-host-key" for req in calls)
    assert "sentinel-host-key" not in json.dumps(result)


def test_local_broker_exposes_only_a_trial_capability():
    from urllib.error import HTTPError
    from urllib.request import ProxyHandler, Request, build_opener

    broker = HostResponses(model="frozen", budget=RequestBudget(total=10, pools={"lead": 10}),
                           count_input=lambda body: 3,
                           send=lambda body: {"usage": {"input_tokens": 3, "output_tokens": 1}},
                           max_output=6, max_requests=1)
    with serve_trial_broker(broker) as (port, capability):
        url = f"http://127.0.0.1:{port}/lead/v1/responses"
        body = json.dumps({"model": "frozen", "input": "offline"}).encode()
        opener = build_opener(ProxyHandler({}))
        with pytest.raises(HTTPError) as denied:
            opener.open(Request(url, data=body), timeout=5)
        assert denied.value.code == 403
        with opener.open(Request(url, data=body, headers={"Authorization": f"Bearer {capability}"}),
                         timeout=5) as response:
            assert json.load(response)["usage"]["input_tokens"] == 3


@pytest.mark.skipif(not os.environ.get("FORGE_BROKER_SMOKE_ADDRESS"), reason="opt-in Docker network smoke")
def test_docker_can_reach_host_broker_without_provider_credentials():
    broker = HostResponses(model="frozen", budget=RequestBudget(total=10, pools={"lead": 10}),
                           count_input=lambda body: 3,
                           send=lambda body: {"usage": {"input_tokens": 3, "output_tokens": 1}},
                           max_output=6, max_requests=1)
    address = os.environ["FORGE_BROKER_SMOKE_ADDRESS"]
    bind_address = os.environ.get("FORGE_BROKER_SMOKE_BIND_ADDRESS", address)
    code = """
import json, os, sys
from urllib.request import ProxyHandler, Request, build_opener
assert not any(k in os.environ for k in ("OPENAI_API_KEY", "SILICONFLOW_API_KEY", "FORGE_EVAL_API_KEY"))
ticket = json.load(sys.stdin)
body = json.dumps({"model": "frozen", "input": "offline connectivity check"}).encode()
req = Request(ticket["url"], data=body, headers={"Authorization": "Bearer " + ticket["capability"]})
with build_opener(ProxyHandler({})).open(req, timeout=8) as response:
    assert json.load(response)["usage"]["input_tokens"] == 3
"""
    with serve_trial_broker(broker, address=bind_address) as (port, capability):
        result = subprocess.run([
            "docker", "run", "--rm", "-i", "--entrypoint", "python",
            "forge-httpx-3367-diagnostic:local", "-c", code,
        ], input=json.dumps({"url": f"http://{address}:{port}/lead/v1/responses",
                            "capability": capability}), capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not os.environ.get("FORGE_MEMORY_SMOKE_WHEELS"), reason="opt-in Docker smoke")
def test_docker_stdio_broker_works_without_network_or_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_EVAL_API_KEY", "host-only-sentinel")
    project = Path(__file__).resolve().parents[1]
    seen = []

    def send(body):
        seen.append(body["input"])
        return {"usage": {"input_tokens": 3, "output_tokens": 1}, "output_text": body["input"]}

    broker = HostResponses(model="frozen", budget=RequestBudget(total=20, pools={"lead": 10, "worker": 10}),
                           count_input=lambda body: 3, send=send, max_output=6, max_requests=2)
    code = """
import os, sys
from concurrent.futures import ThreadPoolExecutor
from llm.relay_stdio import ResponsesPipe
assert not any(k in os.environ for k in ("OPENAI_API_KEY", "SILICONFLOW_API_KEY", "FORGE_EVAL_API_KEY"))
relay = ResponsesPipe(sys.stdin.buffer, sys.stdout.buffer)
with ThreadPoolExecutor(2) as pool:
    answers = list(pool.map(lambda role: relay.request(role, {"model": "frozen", "input": role}), ["lead", "worker"]))
assert [x["output_text"] for x in answers] == ["lead", "worker"]
os._exit(0)
"""
    logfile = tmp_path / "console"
    with logfile.open("wb") as console:
        status = run_with_broker([
            "docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "python",
            "--mount", f"type=bind,src={project / 'llm'},dst=/src/llm,readonly",
            "-e", "PYTHONPATH=/src", "forge-httpx-3367-diagnostic:local", "-c", code,
        ], broker, timeout_sec=45, console=console)
    assert status == 0, logfile.read_text()
    assert sorted(seen) == ["lead", "worker"]
    assert "host-only-sentinel" not in logfile.read_text()


@pytest.mark.parametrize("failure,category,status", [
    ("http", "provider_http", 429),
    ("connect", "provider_connection", "unknown"),
    ("timeout", "provider_timeout", "unknown"),
    ("parse", "provider_response", 200),
])
def test_relay_logs_safe_host_diagnostics_without_leaking_to_child(tmp_path, capsys, failure, category, status):
    import sys
    import httpx
    from openai import OpenAI

    def fake_provider(request):
        if failure == "connect":
            raise httpx.ConnectError("secret-error-sentinel", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-error-sentinel", request=request)
        if failure == "http":
            return httpx.Response(429, json={"error": {"message": "secret-error-sentinel"}})
        return httpx.Response(200, text="secret-error-sentinel")

    child = """
import json, sys
print(json.dumps({'id': 6, 'pool': 'lead', 'body': {'model': 'frozen', 'input': 'secret-request-sentinel'}}), flush=True)
assert json.loads(sys.stdin.readline()) == {'id': 6, 'error': 'rejected'}
"""
    with OpenAI(api_key="secret-key-sentinel", base_url="https://test.invalid/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(fake_provider))) as client:
        broker = bind_host_client(client, model="frozen", budget=RequestBudget(total=16, pools={"lead": 16}),
                                  max_output=8, max_requests=1, contract="output_only")
        logfile = tmp_path / "container-console"
        with logfile.open("wb") as console:
            assert run_with_broker([sys.executable, "-c", child], broker, timeout_sec=10, console=console) == 0
    diagnostic = capsys.readouterr().err
    assert f"request=6 category={category}" in diagnostic and f"http_status={status}" in diagnostic
    if failure in {"connect", "timeout"}:
        assert f"transport_error={'ConnectError' if failure == 'connect' else 'ReadTimeout'}" in diagnostic
    assert "secret-" not in diagnostic
    assert logfile.read_text() == ""


@pytest.mark.skipif(not os.environ.get("FORGE_MEMORY_SMOKE_WHEELS"), reason="opt-in Docker smoke")
def test_independent_docker_snapshot_verdict_controls_memory_publication(tmp_path):
    import hashlib

    commit = "a" * 40
    accepted = b"answer = 2\n"
    proposal = {"type": "feature", "title": "Answer", "content": "Verify the default value",
                "source_run_ids": ["run"], "source_commit": commit, "status": "pending",
                "source_files": ["example.py"],
                "source_file_hashes": {"example.py": hashlib.sha256(accepted).hexdigest()}, "tags": []}
    code = """
import base64, hashlib, json, sys
snapshot = sys.stdin.buffer.read()
files = json.loads(snapshot)["files"]
reward = int(base64.b64decode(files["example.py"]) == b"answer = 2\\n")
print(json.dumps([hashlib.sha256(snapshot).hexdigest(),
    {"verifier_result": {"rewards": {"reward": reward}}},
    {"reward": reward, "parse_status": "ok"}]))
"""

    def verify(payload):
        run = subprocess.run([
            "docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "python",
            "forge-httpx-3367-diagnostic:local", "-c", code,
        ], input=payload, capture_output=True, check=True, timeout=40)
        return tuple(json.loads(run.stdout))

    kwargs = dict(previous=None, repo_id="synthetic/repo", proposals=[proposal], run_id="run",
                  base_commit=commit, run_usage={"run_status": "success"}, verify=verify)
    no = tmp_path / "failed.json"
    assert grade_and_publish(target=no, changed={"example.py": b"answer = 1\n"}, **kwargs) == 0
    assert not no.exists()
    yes = tmp_path / "passed.json"
    assert grade_and_publish(target=yes, changed={"example.py": accepted}, **kwargs) == 1
    assert json.loads(yes.read_bytes())["records"][0]["source_run_ids"] == ["run"]
    with pytest.raises(ValueError, match="unsafe snapshot"):
        seal_snapshot(commit, {"../example.py": accepted})


def test_capture_only_regular_tracked_source(tmp_path):
    import base64

    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "source.py"
    source.write_bytes(b"answer = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Offline", "-c", "user.email=offline@example.invalid",
                    "commit", "-qm", "base"], cwd=repo, check=True)
    root = str(tmp_path / "memory")
    runtime = MemoryRuntime(str(repo), root=root, defer_promotion=True)
    source.write_bytes(b"answer = 2\n")
    runtime.writer.propose(kind="experience", title="Answer", content="Check the answer",
                           related_files=["source.py"], tags=[], run_id="run", repo=str(repo))
    result = capture_candidate(repo, root)
    assert base64.b64decode(result["files"]["source.py"]) == b"answer = 2\n"
    assert result["base_commit"] == runtime.base_commit
    (repo / "extra.py").write_bytes(b"")
    with pytest.raises(ValueError, match="modified tracked"):
        capture_candidate(repo, root)


@pytest.mark.skipif(not os.environ.get("FORGE_HARBOR_SNAPSHOT_SMOKE_ROOT"),
                    reason="opt-in real Harbor verifier smoke")
def test_real_harbor_verifier_publishes_only_the_sealed_click_solution(tmp_path):
    import base64
    import hashlib

    root = Path(os.environ["FORGE_HARBOR_SNAPSHOT_SMOKE_ROOT"])
    task = root / "pr-runtime/pallets__click-3058"
    commit, path = "16fe802a3f96c4c8fa3cd382f1a7577fda0c5321", "src/click/core.py"
    image = "forge-click-3058-verify-base:local"
    # Gold is used only to create a verifier-only fixture; no agent sees it.
    source = subprocess.run([
        "docker", "run", "--rm", "-i", "--network", "none", "--workdir", "/workspace",
        "--entrypoint", "sh", image, "-c",
        "git apply --include=src/click/core.py - && cat src/click/core.py",
    ], input=(task / "solution/patch.diff").read_bytes(),
        capture_output=True, check=True, timeout=60).stdout
    proposal = {"type": "experience", "title": "Fixture memory", "content": "Test the verified source",
                "source_run_ids": ["fixture"], "source_commit": commit, "status": "pending",
                "source_files": [path], "source_file_hashes": {path: hashlib.sha256(source).hexdigest()},
                "tags": []}
    published = tmp_path / "published.json"
    verify = harbor_snapshot_verifier(frozen_task=task, evidence_dir=tmp_path / "evidence",
                                     harbor=root / ".venv/bin/harbor", local_base_image=image)
    trial = tmp_path / "fixture"
    capsule = trial / "agent/forge/memory-capture.json"
    capsule.parent.mkdir(parents=True)
    capsule.write_text(json.dumps({"base_commit": commit, "proposals": [proposal],
                                   "files": {path: base64.b64encode(source).decode("ascii")}}))
    result_file = trial / "result.json"
    result_file.write_text(json.dumps({
        "verifier_result": {"rewards": {"reward": 1}},
        "agent_result": {"metadata": {"memory_capture_status": "captured",
                                     "forge_usage": {"task_id": "fixture", "run_status": "success"}}},
    }))
    assert publish_captured_trial(result_file=result_file, target=published, previous=None,
                                  repo_id="pallets/click", expected_base=commit, verify=verify) == 1
    assert json.loads(published.read_bytes())["records"][0]["source_file_hashes"] == proposal["source_file_hashes"]


@pytest.mark.skipif(not os.environ.get("FORGE_MEMORY_SMOKE_WHEELS"), reason="opt-in offline Docker round trip")
def test_verified_memory_round_trip_in_two_network_disabled_containers():
    project = Path(__file__).resolve().parents[1]
    wheels = Path(os.environ["FORGE_MEMORY_SMOKE_WHEELS"]).resolve()
    assert wheels.is_dir()
    command = [
        "docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "sh",
        "--mount", f"type=bind,src={project / 'project_memory'},dst=/forge/project_memory,readonly",
        "--mount", f"type=bind,src={wheels},dst=/wheels,readonly",
        "forge-httpx-3367-diagnostic:local", "-c",
        "python -m pip install --no-index --no-deps --find-links /wheels --target /tmp/deps PyYAML >&2"
        ' && PYTHONPATH=/forge:/tmp/deps python -c "$1"',
        "memory-smoke",
    ]
    common = """
from pathlib import Path
from project_memory.runtime import MemoryRuntime
from project_memory.transfer import export_verified_memory, import_verified_memory
repo = Path("/tmp/local-repo")
repo.mkdir()
(repo / "example.py").write_text("answer = 1")
runtime = MemoryRuntime(str(repo), root="/tmp/local-memory", defer_promotion=True)
"""
    producer = common + """
import sys
candidate = runtime.writer.propose(kind="experience", title="Default answer", content="Verify default values",
    related_files=["example.py"], tags=[], run_id="synthetic-run", repo=str(repo), commit="synthetic-base")
runtime.writer.resolve(candidate, status="approved", repo=str(repo), verified=True)
sys.stdout.buffer.write(export_verified_memory(runtime.store, "synthetic/repo"))
"""
    consumer = common + """
import sys
assert not runtime.store.read_records()
assert import_verified_memory(runtime.store, "synthetic/repo", sys.stdin.buffer.read()) == 1
assert runtime.retrieval.search("default")[0]["confidence"] == "verified"
assert not runtime.store.read_activity() and not runtime.writer.pending()
"""
    first = subprocess.run([*command, producer], capture_output=True, check=True, timeout=60)
    subprocess.run([*command, consumer], input=first.stdout, capture_output=True, check=True, timeout=60)
