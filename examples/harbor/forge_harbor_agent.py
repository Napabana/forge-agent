from __future__ import annotations

import asyncio
import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import override

import yaml
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from examples.harbor.result_policy import is_benchmark_outcome
from examples.harbor import verifier_provenance


EXAMPLE_DIR = Path(__file__).resolve().parent


class ForgeHarborAgent(BaseAgent):
    """Minimal Harbor adapter for Forge's one-shot ``agent run`` command."""

    DEFAULT_WHEELHOUSE = EXAMPLE_DIR / "wheelhouse"
    DEFAULT_CONFIG = EXAMPLE_DIR / "r2e_forge_runtime.yaml"
    DEFAULT_USAGE_HELPER = EXAMPLE_DIR / "forge_usage_extract.py"

    def __init__(
        self,
        *args,
        wheelhouse_path: str | Path = DEFAULT_WHEELHOUSE,
        config_path: str | Path = DEFAULT_CONFIG,
        usage_helper_path: str | Path = DEFAULT_USAGE_HELPER,
        smoke: bool | str = False,
        agent_timeout_sec: float | None = None,
        memory_bundle_path: str | Path | None = None,
        memory_repo_id: str | None = None,
        offline_group: str | None = None,
        offline_relay: bool | str = False,
        provider_group: str | None = None,
        capture_memory: bool | str = False,
        provenance_package: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._wheelhouse_path = Path(wheelhouse_path)
        self._config_path = Path(config_path)
        self._usage_helper_path = Path(usage_helper_path)
        self._smoke = (
            smoke.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(smoke, str)
            else smoke
        )
        self._agent_timeout_sec = int(agent_timeout_sec or 1800)
        if (memory_bundle_path is None) != (memory_repo_id is None):
            raise ValueError("memory bundle and repository ID must be supplied together")
        self._memory_bundle_path = Path(memory_bundle_path) if memory_bundle_path is not None else None
        self._memory_repo_id = memory_repo_id
        if offline_group not in {None, "A", "B"}:
            raise ValueError("offline_group must be A or B")
        self._offline_group = offline_group
        self._offline_relay = offline_relay is True or (
            isinstance(offline_relay, str) and offline_relay.lower() in {"true", "1"}
        )
        if self._offline_relay and not offline_group:
            raise ValueError("relay rehearsal requires an explicit offline group")
        if provider_group not in {None, "A", "B"} or (provider_group and offline_group):
            raise ValueError("provider group must be A or B and cannot use the offline client")
        self._provider_group = provider_group
        if provenance_package is not None and provenance_package not in verifier_provenance.PACKAGE_ROOTS:
            raise ValueError("unsupported verifier provenance package")
        self._provenance_package = provenance_package
        self._capture_memory = capture_memory is True or (
            isinstance(capture_memory, str) and capture_memory.lower() in {"true", "1"}
        )

    @staticmethod
    @override
    def name() -> str:
        return "forge-agent"

    @override
    def version(self) -> str:
        return "0.1.0+forge.harbor.adapter.3"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        if not self._wheelhouse_path.is_dir():
            raise FileNotFoundError(f"Forge wheelhouse not found: {self._wheelhouse_path}")
        if not self._config_path.is_file():
            raise FileNotFoundError(f"Forge runtime config not found: {self._config_path}")
        if not self._usage_helper_path.is_file():
            raise FileNotFoundError(f"Forge usage helper not found: {self._usage_helper_path}")

        container_wheelhouse = "/tmp/forge-wheelhouse"
        container_wheel = f"{container_wheelhouse}/coding_agent-0.1.0-py3-none-any.whl"
        await environment.upload_dir(self._wheelhouse_path, container_wheelhouse)
        await environment.upload_file(self._config_path, "/tmp/forge-config.yaml")
        await environment.upload_file(self._usage_helper_path, "/tmp/forge-usage-extract.py")
        result = await environment.exec(
            command=(
                "python3 -m venv /opt/forge-venv && "
                "/opt/forge-venv/bin/python -m pip install "
                "--disable-pip-version-check --no-cache-dir --no-index "
                f"--find-links {container_wheelhouse} {container_wheel} && "
                "mkdir -p /logs/agent/forge && "
                "/opt/forge-venv/bin/agent --help "
                "> /logs/agent/forge/install-check.txt"
            ),
            timeout_sec=600,
            user="root",
        )
        if result.return_code != 0:
            detail = result.stderr or result.stdout or "no output"
            raise RuntimeError(f"Forge setup failed ({result.return_code}): {detail[-2000:]}")

    async def _preflight(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(
            command=(
                "test -d /workspace && "
                "test ! -e /tests && "
                "test ! -e /solution && "
                "test -x /opt/forge-venv/bin/agent && "
                "test -f /tmp/forge-config.yaml && "
                "cd /workspace && "
                f"{'python' if self._provenance_package else 'python3'} -m pytest --version"
            ),
            timeout_sec=30,
        )
        if result.return_code != 0:
            raise RuntimeError(
                "Forge preflight failed before model invocation: workspace/hidden-path/install "
                "invariant or target test environment (python3 -m pytest --version) is unavailable"
            )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        config_text = self._config_path.read_text(encoding="utf-8")
        if not (self._offline_group or self._provider_group) and (
            "${FORGE_EVAL_" in config_text or self._memory_bundle_path
        ):
            raise RuntimeError("Core-20 requires a host relay or offline rehearsal")
        await self._preflight(environment)
        if self._memory_bundle_path is not None:
            memory = (yaml.safe_load(config_text) or {}).get("memory", {})
            if (memory.get("enabled") is not True or memory.get("defer_promotion") is not True
                    or memory.get("root") != "/tmp/forge-core20-memory"):
                raise RuntimeError("verified memory import requires the isolated B trial store")
            await environment.upload_file(self._memory_bundle_path, "/tmp/forge-verified-memory.json")
            command = shlex.join((
                "/opt/forge-venv/bin/python", "-m", "project_memory.transfer",
                "--repo", "/workspace", "--root", memory["root"], "--repo-id", self._memory_repo_id,
                "--bundle", "/tmp/forge-verified-memory.json",
            ))
            imported = await environment.exec(command=command, timeout_sec=30)
            if imported.return_code != 0:
                raise RuntimeError("verified memory bundle import failed before model invocation")
        if self._smoke:
            result = await environment.exec(
                command=(
                    "/opt/forge-venv/bin/agent --help "
                    "> /logs/agent/forge/smoke.txt 2>&1 && "
                    "git -C /workspace status --short "
                    ">> /logs/agent/forge/smoke.txt"
                ),
                timeout_sec=30,
            )
            if result.return_code != 0:
                raise RuntimeError(f"Forge smoke failed ({result.return_code})")
            context.metadata = {
                "mode": "smoke",
                "workspace": "/workspace",
                "hidden_paths_absent": True,
                "model_called": False,
            }
            return

        with tempfile.TemporaryDirectory(prefix="forge-harbor-") as temp_dir:
            instruction_path = Path(temp_dir) / "instruction.md"
            instruction_path.write_text(instruction, encoding="utf-8")
            await environment.upload_file(instruction_path, "/tmp/forge-instruction.md")

        launcher = ("/opt/forge-venv/bin/agent",) if self._offline_group is None else (
            "/opt/forge-venv/bin/python", "-m", "evals.harbor_offline", self._offline_group,
        )
        agent_args = (
                "--config",
                "/tmp/forge-config.yaml",
                "run",
                "--repo",
                "/workspace",
                "--task-file",
                "/tmp/forge-instruction.md",
                "--no-stream",
        )
        agent_command = shlex.join((*launcher, *agent_args))
        runtime_env = {"PYTHONUNBUFFERED": "1"}
        if not (self._offline_group or self._provider_group):
            runtime_env.update(self._extra_env)
            api_key = runtime_env.get("SILICONFLOW_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
            if not api_key:
                raise RuntimeError("SILICONFLOW_API_KEY is not available to the adapter")
            runtime_env["SILICONFLOW_API_KEY"] = api_key
        if self._offline_relay or self._provider_group:
            exit_code = await self._run_relay(environment, agent_args)
        else:
            result = await environment.exec(
                command=f"{agent_command} > /logs/agent/forge/console.txt 2>&1",
                cwd="/workspace",
                env=runtime_env,
                timeout_sec=self._agent_timeout_sec,
            )
            exit_code = result.return_code
        usage_result = await environment.exec(
            command=(
                "/opt/forge-venv/bin/python /tmp/forge-usage-extract.py "
                "/logs/agent/forge"
            ),
            timeout_sec=30,
        )
        usage = {}
        if usage_result.return_code == 0 and usage_result.stdout:
            try:
                usage = json.loads(usage_result.stdout)
            except json.JSONDecodeError:
                usage = {"parse_error": "invalid usage helper output"}
        capture_status = "disabled"
        if self._capture_memory:
            memory = (yaml.safe_load(config_text) or {}).get("memory", {})
            if (memory.get("enabled") is not True or memory.get("defer_promotion") is not True
                    or memory.get("root") != "/tmp/forge-core20-memory"):
                raise RuntimeError("Memory capture requires the isolated B trial store")
            capture = await environment.exec(
                command=("/opt/forge-venv/bin/python -m evals.harbor_snapshot_capture"
                         " --repo /workspace --memory-root /tmp/forge-core20-memory"
                         " --output /logs/agent/forge/memory-capture.json"),
                timeout_sec=30,
            )
            capture_status = "captured" if capture.return_code == 0 else "rejected"
        host_usage = getattr(self, "_host_input_usage", None)
        if host_usage and (host_usage["rate_limit_retries"] or host_usage["connect_retries"]):
            usage["host_rate_limit_retries"] = host_usage["rate_limit_retries"]
            usage["host_connect_retries"] = host_usage["connect_retries"]
            if host_usage["unknown_input_calls"]:
                for key in ("input_tokens", "cached_tokens", "uncached_input_tokens",
                            "output_tokens", "reasoning_tokens", "total_tokens"):
                    usage[key] = None
        if isinstance(usage.get("input_tokens"), int):
            context.n_input_tokens = usage["input_tokens"]
        if isinstance(usage.get("cached_tokens"), int):
            context.n_cache_tokens = usage["cached_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            context.n_output_tokens = usage["output_tokens"]
        if isinstance(usage.get("cost_usd"), (int, float)):
            context.cost_usd = float(usage["cost_usd"])
        source_provenance = None
        if self._provenance_package:
            await environment.upload_file(Path(verifier_provenance.__file__), "/tmp/forge-verifier-provenance.py")
            probe = await environment.exec(
                command=shlex.join(("python", "-I", "-S", "/tmp/forge-verifier-provenance.py",
                                    self._provenance_package)), cwd="/workspace", timeout_sec=30,
            )
            if probe.return_code == 0 and probe.stdout:
                try:
                    source_provenance = json.loads(probe.stdout)
                except json.JSONDecodeError:
                    pass
        context.metadata = {
            "mode": ("provider-relay" if self._provider_group else "offline-relay" if self._offline_relay else
                     "offline-rehearsal" if self._offline_group else "agent-run"),
            "workspace": "/workspace",
            "forge_exit_code": exit_code,
            "forge_run_status": usage.get("run_status"),
            "forge_termination_reason": usage.get("termination_reason"),
            "forge_resource_reason": usage.get("resource_reason"),
            "forge_usage": usage,
            "budget_contract": os.environ.get("FORGE_EVAL_BUDGET_CONTRACT") if self._provider_group else None,
            "host_input_usage": host_usage,
            "source_provenance": source_provenance,
            "memory_capture_status": capture_status,
        }
        # max_steps/GAVE_UP 是合法模型结果，Harbor 仍应运行 verifier 记录真实 reward；
        # Provider、基础设施、取消或缺少可信 Trace 终态继续按 adapter exception 收口。
        if exit_code != 0 and not is_benchmark_outcome(usage):
            raise RuntimeError(
                f"Forge agent run failed with exit code {exit_code}; "
                f"run_status={usage.get('run_status')!r}, "
                f"termination_reason={usage.get('termination_reason')!r}; "
                "see /logs/agent/forge/console.txt"
            )

    async def _run_relay(self, environment: BaseEnvironment, agent_args: tuple[str, ...]) -> int:
        from evals.harbor_offline import OfflineClient, input_count, offline_budget
        from examples.harbor.docker_stdio import run_with_broker
        from examples.harbor.host_responses import TOTAL_TOKENS, HostResponses, bind_host_client, create_trial_client

        resolve = getattr(getattr(environment, "_platform", None), "_resolve_service_container", None)
        if not callable(resolve):
            raise RuntimeError("this Harbor Docker version does not expose the main container resolver")
        container = await resolve("main")
        if not container or not all(ch.isascii() and (ch.isalnum() or ch in "_.-") for ch in container):
            raise RuntimeError("invalid Harbor main container ID")
        limits = dict(model="gpt-5.6-luna", budget=offline_budget(self._provider_group or self._offline_group),
                      max_output=8192, max_requests=128)
        with tempfile.TemporaryDirectory(prefix="forge-relay-") as temp:
            console_path = Path(temp) / "console.txt"
            command = ["docker", "exec", "-i", "--workdir", "/workspace", "--", container,
                       "/opt/forge-venv/bin/python", "-m", "evals.harbor_relay", *agent_args]
            with console_path.open("wb") as console:
                def run(broker):
                    return run_with_broker(command, broker, timeout_sec=self._agent_timeout_sec, console=console)

                if self._provider_group:
                    with create_trial_client(os.environ) as client:
                        contract = os.environ.get("FORGE_EVAL_BUDGET_CONTRACT", TOTAL_TOKENS)
                        broker = bind_host_client(client, contract=contract, retry_rate_limit=True,
                                                  retry_connect=True, **limits)
                        status = await asyncio.to_thread(run, broker)
                        self._host_input_usage = broker.input_usage()
                else:
                    fake = OfflineClient()
                    broker = HostResponses(count_input=input_count, send=lambda body: fake.create(**body), **limits)
                    status = await asyncio.to_thread(run, broker)
            await environment.upload_file(console_path, "/logs/agent/forge/console.txt")
        return status
