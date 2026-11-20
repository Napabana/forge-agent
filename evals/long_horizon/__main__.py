"""Run the frozen local fault-injection scenarios and write a JSON report."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


SCENARIOS = {
    "model_action_committed": ("test_durable_run_manager", "test_direct_resume_replays_saved_finish_without_calling_model"),
    "tool_result_replay": ("test_durable_run_manager", "test_direct_resume_reuses_tool_result_and_refuses_changed_workspace"),
    "process_exit_after_started": ("test_durable_run_manager", "test_crashed_process_requires_explicit_resolution"),
    "file_write_result_lost": ("test_durable_run_manager", "test_lost_file_tool_result_can_be_inspected_without_repeating_write"),
    "verified_file_retry": ("test_durable_run_manager", "test_verified_file_retry_links_new_operation_to_original"),
    "cooperative_pause": ("test_durable_run_manager", "test_pause_is_acknowledged_between_steps_and_resumes"),
    "cli_checkpoint_resume": ("test_durable_run_manager", "test_cli_resume_reuses_saved_action"),
    "agent_and_acceptance_results": ("test_durable_run_manager", "test_runner_persists_agent_and_acceptance_results"),
    "chat_round_resume": ("test_chat", "test_persisted_chat_reopens_and_resumes_its_interrupted_round"),
    "chat_rewind_fork": ("test_chat", "test_rewind_uses_saved_round_after_clear_and_forks_the_original"),
    "cli_isolated_pause": ("test_runtime_resume_acceptance", "test_isolated_run_can_pause_and_resume_from_cli"),
    "isolated_worktree_resume": ("test_runtime_resume_acceptance", "test_isolated_resume_reuses_original_checkout_and_tool_result"),
    "pending_acceptance_resume": ("test_runtime_resume_acceptance", "test_resume_pending_path_acceptance_without_model_or_tool_replay"),
    "worker_parent_resume": ("test_runtime_resume_acceptance", "test_worker_resume_uses_saved_action_after_lead_and_worker_interruption"),
    "active_compaction_resume": ("test_runtime_resume_acceptance", "test_active_compaction_resume_reuses_archived_view_without_recompacting"),
    "pr_response_lost": ("test_github_issue_delivery", "test_durable_delivery_records_unknown_pr_response"),
    "verified_push_retry": ("test_github_issue_delivery", "test_durable_push_retry_requires_verified_absent_ref"),
    "mcp_receipt_query": ("test_mcp_runtime_recovery", "test_mcp_only_calls_the_configured_read_only_receipt_query"),
    "archive_blob_tampered": ("test_context_archive", "test_archive_captures_history_and_projects_summary_atomically"),
    "archive_import_and_rewrite": ("test_context_archive", "test_archive_import_is_idempotent_and_rewrite_validates_retained_source"),
    "cross_scope_archive_bundle": ("test_context_archive", "test_archive_bundle_copies_transitive_sources_without_replacing_destination"),
    "provider_cache_read_unknown": ("test_prefix_cache_audit", "test_final_request_audit_tracks_tail_and_missing_provider_cache_data"),
    "provider_sdk_physical_retry": ("test_prefix_cache_audit", "test_openai_sdk_physical_retry_is_observed_at_local_transport"),
    "owner_revision_fenced": ("test_durable_runtime_store", "test_claim_fences_writers_and_revisions"),
    "worker_scope_fenced": ("test_durable_runtime_store", "test_shared_worker_owner_is_fenced_after_lead_releases_scope"),
    "started_outcome_unknown": ("test_durable_runtime_store", "test_started_operation_becomes_unknown_on_takeover"),
    "operation_boundary_matrix": ("test_durable_runtime_store", "test_tool_journal_recovery_at_each_boundary"),
    "explicit_boot_takeover": ("test_durable_runtime_store", "test_different_boot_requires_matching_explicit_takeover"),
    "onboarding_validation_failed": ("test_onboarding", "test_init_checks_provider_before_writing_and_preserves_config"),
    "onboarding_keyring_reference": ("test_onboarding", "test_init_persists_only_keyring_reference"),
    "onboarding_cancellation_and_trust": ("test_onboarding", "test_interactive_init_records_trust_only_on_confirmation"),
    "workspace_trust_revoked": ("test_onboarding", "test_workspace_trust_grant_and_revocation"),
    "skill_update_process_exit": ("test_resource_commands", "test_skill_update_recovers_fatal_interruption_and_serializes_processes"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error("output file already exists")
    tests = [f"tests/{module}.py::{test}" for module, test in SCENARIOS.values()]
    directory = Path(tempfile.mkdtemp(prefix=output.stem + "-artifacts-"))
    junit = directory / "results.xml"
    evidence_file = directory / "durable-evidence.jsonl"
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "evals.long_horizon.pytest_plugin",
                          "--basetemp", str(directory / "trials"), "--forge-trial-evidence", str(evidence_file),
                          "-o", "junit_family=xunit1", "--junitxml", str(junit), *tests], cwd=root, check=False)
    cases = {item.attrib["name"]: item for item in ET.parse(junit).iter("testcase")} if junit.exists() else {}
    evidence = (
        {row["node_id"]: row for row in map(json.loads, evidence_file.read_text(encoding="utf-8").splitlines())}
        if evidence_file.is_file() else {}
    )
    trials = []
    for scenario, (module, test) in SCENARIOS.items():
        case = cases.get(test)
        status = "not_executed"
        if case is not None:
            status = next((value for value in ("error", "failure", "skipped") if case.find(value) is not None), "passed")
        fixture = f"tests/{module}.py"
        node_id = f"{fixture}::{test}"
        observed = evidence.get(node_id)
        trials.append({
            "task_id": scenario, "fixture": node_id,
            "fixture_sha256": hashlib.sha256((root / fixture).read_bytes()).hexdigest(),
            "failure_injection": scenario, "verifier": status,
            "elapsed_seconds": float(case.attrib["time"]) if case is not None else None,
            "durable_evidence": observed,
        })
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    snapshot = hashlib.sha256()
    objects = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                              "agent", "context", "durable_runtime", "entry", "evals/long_horizon", "harness",
                              "llm", "runtime", "skills", "tools", "config/schema.py", "config/credentials.py", "pyproject.toml",
                              *[item.split("::")[0] for item in tests]],
                             cwd=root, capture_output=True, check=False)
    if objects.returncode == 0:
        for raw in sorted(set(objects.stdout.split(b"\0")) - {b""}):
            filename = root / raw.decode("utf-8")
            if filename.is_file():
                snapshot.update(raw + b"\0" + hashlib.sha256(filename.read_bytes()).digest())
    report = {"suite": "forge-local-reliability-v2", "git_commit": commit.stdout.strip() or None,
              "artifacts": str(directory),
              "source_snapshot_digest": snapshot.hexdigest() if objects.returncode == 0 else None,
              "pytest_exit_code": run.returncode, "passed": sum(item["verifier"] == "passed" for item in trials),
              "planned": len(trials), "trials": trials}
    report["evidence_failures"] = []
    report["measurements"] = {}
    for trial in trials:
        trial["git_commit"] = report["git_commit"]
        trial["source_snapshot_digest"] = report["source_snapshot_digest"]
        observed = trial["durable_evidence"]
        if observed is None or any("collection_error" in store for store in observed["stores"]):
            report["evidence_failures"].append(trial["task_id"])
        if observed is None:
            continue
        for key, value in observed["measurements"].items():
            if type(value) is int:
                metric = report["measurements"].setdefault(key, {"sum": 0, "trials": 0})
                metric["sum"] += value
                metric["trials"] += 1
    for name, metric in report["measurements"].items():
        if name.endswith("_correct") or name.endswith("_detected") or name == "checkpoint_recovery":
            metric["rate"] = metric["sum"] / metric["trials"]
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0 if (run.returncode == 0 and report["passed"] == report["planned"]
                 and report["source_snapshot_digest"] and report["git_commit"]
                 and not report["evidence_failures"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
