#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
install_root="${XDG_DATA_HOME:-$HOME/.local/share}/forge-agent"
venv="$install_root/venv"
bin_dir="$HOME/.local/bin"
python=python3
command -v python3.11 >/dev/null 2>&1 && python=python3.11
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
  echo 'Forge requires Python 3.11 or newer.' >&2
  exit 1
}

"$python" -m venv "$venv"
build_source="$(mktemp -d)"
trap 'rm -rf -- "$build_source"' EXIT
tar -C "$repo_root" --exclude='evals/results' --exclude='*/__pycache__' \
  --exclude='*.pyc' -cf - \
  pyproject.toml README.md agent config context entry evals experience harness ipc llm \
  mcp_integration runtime skills task tools | tar -C "$build_source" -xf -
"$venv/bin/python" -m pip install "$build_source"
mkdir -p "$bin_dir"
ln -sfn "$venv/bin/forgeAgent" "$bin_dir/forgeAgent"

path_line='export PATH="$HOME/.local/bin:$PATH"'
touch "$HOME/.bashrc"
grep -Fqx "$path_line" "$HOME/.bashrc" || printf '\n%s\n' "$path_line" >> "$HOME/.bashrc"

printf 'Installed forgeAgent. Open a new Bash terminal, then run: forgeAgent --help\n'
printf 'Put private config in ~/.config/forge-agent/config.yaml and API keys in ~/.config/forge-agent/env.\n'
