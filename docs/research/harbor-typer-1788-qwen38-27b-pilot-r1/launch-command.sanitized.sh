#!/usr/bin/env bash
set -euo pipefail
set -a
source "$HOME/.config/forge-agent/env"
set +a
export PYTHONPATH=<FORGE_HOST_ROOT>
exec <R2E_WORKSPACE>/.venv/bin/harbor run \
  --path <R2E_WORKSPACE>/pr-runtime/tiangolo__typer-1788 \
  --agent forge_harbor_agent:ForgeHarborAgent \
  --agent-kwarg agent_timeout_sec=5400 \
  --agent-kwarg config_path=<FORGE_HOST_ROOT>/r2e_forge_runtime_qwen38_27b_pilot_r1.yaml \
  --env docker \
  --n-concurrent 1 \
  --n-attempts 1 \
  --max-retries 0 \
  --jobs-dir <R2E_WORKSPACE>/experiments/first-python-task/forge-real \
  --job-name typer-1788-qwen38-27b-pilot-r1 \
  --allow-agent-host api.siliconflow.cn \
  --yes \
  --no-stream
