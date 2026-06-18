#!/usr/bin/env bash
# 启动 forge-agent：激活 venv + 注入 ~/.learn-claude-code/.env + 运行 agent CLI
#
# 用法：
#   scripts/start.sh chat                      # 交互式 chat（默认）
#   scripts/start.sh run --task "修复测试"
#   scripts/start.sh --help
#
# API key / model / base_url 从 ~/learn-claude-code/.env 读取（GLM-5 via z.ai）。
# 如需改用其它模型，编辑该 .env 的 MODEL_ID / ANTHROPIC_BASE_URL 段。
set -euo pipefail

REPO="/home/jfm/forge-agent"
VENV="/home/jfm/.venv"
ENV_FILE="${FORGE_ENV_FILE:-$HOME/learn-claude-code/.env}"

# 1. 激活虚拟环境
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 2. 注入 .env（仅设置未在 shell 中导出的变量）
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  echo "⚠ 未找到 $ENV_FILE —— LLM 配置（api_key/model/base_url）将缺失" >&2
fi

# 3. 进入仓库
cd "$REPO"

# 4. 透传参数给 agent CLI
exec python -m entry.cli "$@"
