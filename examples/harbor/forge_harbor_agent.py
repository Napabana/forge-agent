from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import override

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from examples.harbor.result_policy import is_benchmark_outcome


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
                "python3 -m pytest --version"
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
        await self._preflight(environment)
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

        agent_command = " ".join(
            shlex.quote(value)
            for value in (
                "/opt/forge-venv/bin/agent",
                "--config",
                "/tmp/forge-config.yaml",
                "run",
                "--repo",
                "/workspace",
                "--task-file",
                "/tmp/forge-instruction.md",
                "--no-stream",
            )
        )
        runtime_env = {**self._extra_env, "PYTHONUNBUFFERED": "1"}
        api_key = runtime_env.get("SILICONFLOW_API_KEY") or os.environ.get(
            "SILICONFLOW_API_KEY"
        )
        if not api_key:
            raise RuntimeError("SILICONFLOW_API_KEY is not available to the adapter")
        runtime_env["SILICONFLOW_API_KEY"] = api_key
        result = await environment.exec(
            command=f"{agent_command} > /logs/agent/forge/console.txt 2>&1",
            cwd="/workspace",
            env=runtime_env,
            timeout_sec=self._agent_timeout_sec,
        )
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
        if isinstance(usage.get("input_tokens"), int):
            context.n_input_tokens = usage["input_tokens"]
        if isinstance(usage.get("cached_tokens"), int):
            context.n_cache_tokens = usage["cached_tokens"]
        if isinstance(usage.get("output_tokens"), int):
            context.n_output_tokens = usage["output_tokens"]
        if isinstance(usage.get("cost_usd"), (int, float)):
            context.cost_usd = float(usage["cost_usd"])
        context.metadata = {
            "mode": "agent-run",
            "workspace": "/workspace",
            "forge_exit_code": result.return_code,
            "forge_run_status": usage.get("run_status"),
            "forge_termination_reason": usage.get("termination_reason"),
            "forge_resource_reason": usage.get("resource_reason"),
            "forge_usage": usage,
        }
        # max_steps/GAVE_UP 是合法模型结果，Harbor 仍应运行 verifier 记录真实 reward；
        # Provider、基础设施、取消或缺少可信 Trace 终态继续按 adapter exception 收口。
        if result.return_code != 0 and not is_benchmark_outcome(usage):
            raise RuntimeError(
                f"Forge agent run failed with exit code {result.return_code}; "
                f"run_status={usage.get('run_status')!r}, "
                f"termination_reason={usage.get('termination_reason')!r}; "
                "see /logs/agent/forge/console.txt"
            )
