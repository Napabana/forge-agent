"""Read-only checks against postconditions captured before a native file tool runs."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from tools.base import ToolResult
from tools.file_tool import FileEditTool, FileWriteTool, _resolve_workspace_path


def _file_state(path: Path) -> str | None:
    if not path.exists():
        return "absent"
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def file_postcondition(tool, params: dict) -> dict | None:
    if type(tool) not in {FileEditTool, FileWriteTool} or tool._workspace is None:
        return None
    path = _resolve_workspace_path(params["path"], tool._workspace)
    if isinstance(path, ToolResult):
        return None
    try:
        if type(tool) is FileWriteTool:
            before = _file_state(path)
            content = params["content"].replace("\n", os.linesep).encode("utf-8")
        else:
            with path.open("rb") as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
            old = params["old_text"].encode("utf-8")
            if len(raw) > 16 * 1024 * 1024 or not old or raw.count(old) != 1:
                return None
            before = hashlib.sha256(raw).hexdigest()
            content = raw.replace(old, params["new_text"].encode("utf-8"), 1)
    except (OSError, ValueError):
        return None
    if before is None:
        return None
    return {"workspace": str(tool._workspace), "path": path.relative_to(tool._workspace).as_posix(),
            "before": before, "after": hashlib.sha256(content).hexdigest()}


def inspect_file_operation(operation, project_identity) -> dict | None:
    if operation["name"] not in {"file_edit", "file_write"} or not operation["expected_postcondition_json"]:
        return None
    from project_memory.identity import ProjectIdentityResolver

    expected = json.loads(operation["expected_postcondition_json"])
    workspace = Path(expected["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir() or ProjectIdentityResolver.resolve(workspace) != project_identity:
        return {"outcome": "unknown", "reason": "workspace identity cannot be verified"}
    path = _resolve_workspace_path(expected["path"], workspace)
    if isinstance(path, ToolResult) or path != workspace / expected["path"]:
        return {"outcome": "unknown", "reason": "target path no longer resolves to the saved location"}
    try:
        current = _file_state(path)
    except OSError:
        current = None
    outcome = "unknown"
    if expected["before"] != expected["after"]:
        if current == expected["after"]:
            outcome = "applied"
        elif current == expected["before"]:
            outcome = "precondition_matches"
    return {"outcome": outcome, "observed_sha256": current, "expected": expected}


def inspect_git_commit(operation, project_identity) -> dict | None:
    if operation["name"] != "git_commit" or not operation["expected_postcondition_json"]:
        return None
    from project_memory.identity import ProjectIdentityResolver

    expected = json.loads(operation["expected_postcondition_json"])
    workspace = Path(expected["workspace"])
    if not workspace.is_absolute() or not workspace.is_dir() or ProjectIdentityResolver.resolve(workspace) != project_identity:
        return {"outcome": "unknown", "reason": "workspace identity cannot be verified"}
    branch = "refs/heads/" + expected["branch"]
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H%x00%P%x00%T%x00%s", branch, "--"],
            cwd=workspace, capture_output=True, text=True, timeout=5, check=False,
        )
        commit, parents, tree, subject = result.stdout.strip().split("\0", 3)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"outcome": "unknown", "reason": "branch commit could not be inspected"}
    applied = result.returncode == 0 and (parents, tree, subject) == (
        expected["parent"], expected["tree"], expected["message"],
    )
    return {"outcome": "applied" if applied else "unknown", "observed_commit": commit,
            "observed_parents": parents, "observed_tree": tree}


def inspect_remote_delivery(operation, project_identity) -> dict | None:
    name = operation["name"]
    if name not in {"git_push", "github_create_pr"} or not operation["expected_postcondition_json"]:
        return None
    from entry.github_issue import _get_github_client, _github_git_env, _run_git
    from project_memory.identity import ProjectIdentityResolver

    expected = json.loads(operation["expected_postcondition_json"])
    target = expected["target_commit"]
    if not isinstance(target, str) or not target or not all(char in "0123456789abcdef" for char in target):
        return {"outcome": "unknown", "reason": "missing expected commit"}
    if name == "git_push":
        workspace = Path(expected["workspace"])
        if not workspace.is_absolute() or not workspace.is_dir() or ProjectIdentityResolver.resolve(workspace) != project_identity:
            return {"outcome": "unknown", "reason": "workspace identity cannot be verified"}
        ref = "refs/heads/" + expected["branch"]
        success, response = _run_git(["ls-remote", "--heads", "origin", ref], cwd=str(workspace), env=_github_git_env())
        refs = [line.split() for line in response.splitlines()]
        observed = next((line[0] for line in refs if len(line) == 2 and line[1] == ref), None) if success else None
        outcome = "applied" if target == observed else "not_applied" if success and not refs else "unknown"
        return {"outcome": outcome, "remote_ref": ref, "observed_commit": observed}
    repository = expected["repo"]
    if project_identity.key.casefold() != f"github.com/{repository.casefold()}":
        return {"outcome": "unknown", "reason": "GitHub repository does not match the project identity"}
    client = None
    try:
        client = _get_github_client()
        repo = client.get_repo(repository)
        head = f"{repository.split('/')[0]}:{expected['head']}"
        for base in dict.fromkeys([expected["base"], "master"]):
            for pull_request in repo.get_pulls(state="all", head=head, base=base):
                if pull_request.head.sha == target and pull_request.head.ref == expected["head"] and pull_request.base.ref == base:
                    return {"outcome": "applied", "pull_request_number": pull_request.number,
                            "head_commit": pull_request.head.sha, "base_branch": base,
                            "url": pull_request.html_url}
    except Exception as exc:
        return {"outcome": "unknown", "reason": f"GitHub query failed ({type(exc).__name__})"}
    finally:
        if client is not None:
            client.close()
    return {"outcome": "unknown", "reason": "no verified pull request matched the saved commit"}


def mcp_postcondition(tool, params: dict) -> dict | None:
    from dataclasses import asdict

    from agent.trace_v2 import is_sensitive_trace_key, redact_trace_value
    from mcp_integration.adapter import MCPToolAdapter
    from .run_manager import digest, encode

    if type(tool) is not MCPToolAdapter:
        return None
    server = next((server for server in tool._manager.config.servers if server.id == tool.server_id), None)
    query = server.recovery_queries.get(tool.remote_name) if server else None
    if not query or not server.trust_read_only_annotations:
        return None
    field = query["argument"]
    value = params.get(field)
    if (type(value) not in {str, int} or is_sensitive_trace_key(field)
            or isinstance(value, str) and (not value or len(value) > 200 or redact_trace_value(value) != value)):
        return None
    return {"server": server.id, "server_digest": digest(encode(asdict(server))),
            "origin_tool": tool.remote_name, "query_tool": query["query_tool"],
            "argument": field, "correlation": value}


def inspect_mcp(operation, config) -> dict | None:
    from dataclasses import asdict, replace

    from mcp_integration.manager import MCPClientManager
    from .run_manager import digest, encode

    if not operation["name"].startswith("mcp__") or not operation["expected_postcondition_json"]:
        return None
    expected = json.loads(operation["expected_postcondition_json"])
    server = next((server for server in config.servers if server.id == expected.get("server")), None)
    if (not server or not server.enabled or not server.trust_read_only_annotations
            or digest(encode(asdict(server))) != expected.get("server_digest")):
        return {"outcome": "unknown", "reason": "saved MCP server configuration differs"}
    manager = MCPClientManager(replace(config, enabled=True, servers=(server,)))
    try:
        manager.start()
        query = next((item for item in manager.tools if item.remote_name == expected["query_tool"]), None)
        if query is None or query.annotations.get("read_only_hint") is not True:
            return {"outcome": "unknown", "reason": "MCP did not advertise a read-only query"}
        field = expected["argument"]
        response = manager.call_tool(server.id, query.remote_name, {field: expected["correlation"]},
                                     timeout_seconds=server.timeout_seconds)
        receipt = getattr(response, "structured_content", None) or {}
        if (getattr(response, "is_error", False) or not isinstance(receipt, dict)
                or receipt.get(field) != expected["correlation"]
                or receipt.get("outcome") not in {"applied", "not_applied"}):
            return {"outcome": "unknown", "reason": "query did not return a matching receipt"}
        return {"outcome": receipt["outcome"], "server": server.id, "query_tool": query.remote_name,
                "receipt_digest": digest(encode(receipt))}
    except Exception as exc:
        return {"outcome": "unknown", "reason": f"MCP query failed ({type(exc).__name__})"}
    finally:
        manager.close()
