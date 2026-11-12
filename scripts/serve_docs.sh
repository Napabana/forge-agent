#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docs_python="${FORGE_AGENT_DOCS_PYTHON:-$HOME/.venvs/forge-agent-docs/bin/python}"

if [[ ! -x "$docs_python" ]]; then
  echo "Missing docs environment. Run: python3 -m venv ~/.venvs/forge-agent-docs && ~/.venvs/forge-agent-docs/bin/pip install -r requirements-docs.txt" >&2
  exit 1
fi

cd "$repo_root"
exec "$docs_python" -m zensical serve "$@"
