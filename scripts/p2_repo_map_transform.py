from __future__ import annotations

from pathlib import Path

ROOT = Path('.')


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{path}: expected one replacement, found {count}: {old[:80]!r}')
    target.write_text(text.replace(old, new, 1), encoding='utf-8')


replace_once(
    'agent/core.py',
    'from context.history import ConversationHistory\nfrom context.repo_map import RepoMap\nfrom context.repository_state import repository_fingerprint\n',
    'from context.history import ConversationHistory\nfrom context.incremental_repo_map import PersistentRepoMap\nfrom context.repo_map import RepoMap\nfrom context.repository_state import repository_fingerprint\n',
)
replace_once(
    'agent/core.py',
    '    hooks: Hooks | None = None\n    prepare_next_turn: PrepareNextTurn | None = None\n',
    '    hooks: Hooks | None = None\n    prepare_next_turn: PrepareNextTurn | None = None\n'
    '    # Production defaults to persistent incremental Repo Map. Other modes are\n'
    '    # retained as controlled ablation variants.\n'
    '    repo_map_mode: str = "incremental"\n'
    '    repo_map_cache_dir: str | None = None\n',
)
replace_once(
    'agent/core.py',
    '        self._repo_map_cache_key: str | None = None\n'
    '        self._repo_map_force_refresh = False\n'
    '        self._prepared_history_override: tuple[LLMMessage, ...] | None = None\n',
    '        self._repo_map_cache_key: str | None = None\n'
    '        self._repo_map_force_refresh = False\n'
    '        self._repo_map_sync_requested = False\n'
    '        self._repo_map_build_seconds = 0.0\n'
    '        self._repo_map_build_calls = 0\n'
    '        self._prepared_history_override: tuple[LLMMessage, ...] | None = None\n',
)
replace_once(
    'agent/core.py',
    '        existed = hasattr(self, "_repo_map_cache")\n'
    '        if existed:\n'
    '            del self._repo_map_cache\n'
    '        self._repo_map_force_refresh = True\n'
    '        return existed\n\n'
    '    def run(\n',
    '        existed = hasattr(self, "_repo_map_cache")\n'
    '        if existed:\n'
    '            del self._repo_map_cache\n'
    '        if self._repo_map_mode() == "incremental":\n'
    '            self._repo_map_sync_requested = True\n'
    '            self._repo_map_force_refresh = False\n'
    '        else:\n'
    '            self._repo_map_force_refresh = True\n'
    '        return existed\n\n'
    '    def _repo_map_mode(self) -> str:\n'
    '        mode = (self._cfg.repo_map_mode or "incremental").strip().lower()\n'
    '        if mode not in {"none", "static", "query_aware", "incremental"}:\n'
    '            raise ValueError(f"Unsupported repo_map_mode: {self._cfg.repo_map_mode!r}")\n'
    '        return mode\n\n'
    '    def _new_repo_map(self, repo_path: str | Path) -> RepoMap:\n'
    '        if self._repo_map_mode() == "incremental":\n'
    '            return PersistentRepoMap(repo_path, cache_dir=self._cfg.repo_map_cache_dir)\n'
    '        return RepoMap(repo_path)\n\n'
    '    @property\n'
    '    def repo_map_telemetry(self) -> dict[str, object]:\n'
    '        index_metrics = getattr(getattr(self, "_repo_map_instance", None), "metrics", None)\n'
    '        return {\n'
    '            "mode": self._repo_map_mode(),\n'
    '            "build_seconds": self._repo_map_build_seconds,\n'
    '            "build_calls": self._repo_map_build_calls,\n'
    '            "index": index_metrics.snapshot() if index_metrics is not None else None,\n'
    '        }\n\n'
    '    def run(\n',
)
replace_once(
    'agent/core.py',
    '            self._repo_map_force_refresh = False\n'
    '            self._repo_map_cache_key = cache_key\n'
    '            self._repo_map_instance = RepoMap(task.repo_path)\n',
    '            self._repo_map_force_refresh = False\n'
    '            self._repo_map_sync_requested = False\n'
    '            self._repo_map_cache_key = cache_key\n'
    '            self._repo_map_instance = self._new_repo_map(task.repo_path)\n',
)
replace_once(
    'agent/core.py',
    '                    if observation.is_success():\n'
    '                        successful_write = True\n'
    '                        last_write_step = step\n'
    '                        self.invalidate_repo_map_cache(task.repo_path)\n',
    '                    if observation.is_success():\n'
    '                        successful_write = True\n'
    '                        last_write_step = step\n'
    '                        if self._repo_map_mode() == "incremental":\n'
    '                            if hasattr(self, "_repo_map_cache"):\n'
    '                                del self._repo_map_cache\n'
    '                            changed_path = tc.params.get("path")\n'
    '                            try:\n'
    '                                if changed_path:\n'
    '                                    repo_map.update_paths([changed_path])  # type: ignore[attr-defined]\n'
    '                                    self._repo_map_sync_requested = False\n'
    '                                else:\n'
    '                                    self._repo_map_sync_requested = True\n'
    '                            except Exception as exc:\n'
    '                                logger.warning("Incremental Repo Map update failed: %s", exc)\n'
    '                                self._repo_map_sync_requested = True\n'
    '                        else:\n'
    '                            self.invalidate_repo_map_cache(task.repo_path)\n',
)
replace_once(
    'agent/core.py',
    '        schemas = tuple(self._registry.get_schemas())\n'
    '        if not hasattr(self, "_repo_map_cache"):\n'
    '            map_budget = token_budget.default_plan().repo_map\n'
    '            if self._repo_map_force_refresh:\n'
    '                self._repo_map_cache = repo_map.build(\n'
    '                    budget=map_budget,\n'
    '                    force_refresh=True,\n'
    '                    query=getattr(self, "_repo_map_query", None),\n'
    '                )\n'
    '            else:\n'
    '                self._repo_map_cache = repo_map.build(\n'
    '                    budget=map_budget,\n'
    '                    query=getattr(self, "_repo_map_query", None),\n'
    '                )\n'
    '            self._repo_map_force_refresh = False\n'
    '            self._repo_map_cache_query = getattr(self, "_repo_map_query", None)\n\n'
    '        repo_map_content = self._repo_map_cache\n'
    '        system_content = build_system_prompt(\n'
    '            repo_path=getattr(self, "_current_repo_path", "."),\n'
    '            tools=list(schemas),\n'
    '            repo_summary=repo_map_content,\n'
    '        )\n',
    '        schemas = tuple(self._registry.get_schemas())\n'
    '        mode = self._repo_map_mode()\n'
    '        if not hasattr(self, "_repo_map_cache"):\n'
    '            map_budget = token_budget.default_plan().repo_map\n'
    '            query = None if mode == "static" else getattr(self, "_repo_map_query", None)\n'
    '            started = time.perf_counter()\n'
    '            if mode == "none":\n'
    '                self._repo_map_cache = ""\n'
    '            elif mode == "incremental":\n'
    '                if self._repo_map_sync_requested:\n'
    '                    repo_map.sync()  # type: ignore[attr-defined]\n'
    '                self._repo_map_cache = repo_map.build(budget=map_budget, query=query)\n'
    '            elif self._repo_map_force_refresh:\n'
    '                self._repo_map_cache = repo_map.build(\n'
    '                    budget=map_budget, force_refresh=True, query=query\n'
    '                )\n'
    '            else:\n'
    '                self._repo_map_cache = repo_map.build(budget=map_budget, query=query)\n'
    '            self._repo_map_build_seconds += time.perf_counter() - started\n'
    '            self._repo_map_build_calls += int(mode != "none")\n'
    '            self._repo_map_force_refresh = False\n'
    '            self._repo_map_sync_requested = False\n'
    '            self._repo_map_cache_query = getattr(self, "_repo_map_query", None)\n\n'
    '        repo_map_content = self._repo_map_cache\n'
    '        system_content = build_system_prompt(\n'
    '            repo_path=getattr(self, "_current_repo_path", "."),\n'
    '            tools=list(schemas),\n'
    '            repo_summary=repo_map_content if mode != "none" else None,\n'
    '        )\n',
)

replace_once(
    'agent/runner.py',
    '        self._agent_key = (\n'
    '            id(self.config.hooks),\n'
    '            id(self.config.cancel_event),\n'
    '            id(self.config.prepare_next_turn),\n'
    '            id(None),\n'
    '        )\n',
    '        self._agent_key = (\n'
    '            id(self.config.hooks),\n'
    '            id(self.config.cancel_event),\n'
    '            id(self.config.prepare_next_turn),\n'
    '            id(None),\n'
    '            self.config.repo_map_mode,\n'
    '            self.config.repo_map_cache_dir,\n'
    '        )\n',
)
replace_once(
    'agent/runner.py',
    '            id(config.prepare_next_turn),\n'
    '            id(request.permission),\n'
    '        )\n',
    '            id(config.prepare_next_turn),\n'
    '            id(request.permission),\n'
    '            config.repo_map_mode,\n'
    '            config.repo_map_cache_dir,\n'
    '        )\n',
)
replace_once(
    'agent/runner.py',
    '            self.agent._repo_map_force_refresh = False\n'
    '            self.agent._repo_map_cache_key = cache_key\n'
    '            self.agent._repo_map_instance = RepoMap(task.repo_path)\n',
    '            self.agent._repo_map_force_refresh = False\n'
    '            self.agent._repo_map_sync_requested = False\n'
    '            self.agent._repo_map_cache_key = cache_key\n'
    '            self.agent._repo_map_instance = self.agent._new_repo_map(task.repo_path)\n',
)
replace_once(
    'agent/runner.py',
    '        repo_map = getattr(self.agent, "_repo_map_instance", RepoMap(task.repo_path))\n',
    '        repo_map = getattr(self.agent, "_repo_map_instance", self.agent._new_repo_map(task.repo_path))\n',
)

replace_once(
    'agent/prompt.py',
    '## Repository\nPath: {repo_path}\n{repo_summary}\n\n## Available tools\n{tool_descriptions}\n',
    '## Available tools\n{tool_descriptions}\n\n## Repository\nPath: {repo_path}\n{repo_summary}\n',
)

path = ROOT / 'tests/test_repo_map_improvements.py'
text = path.read_text(encoding='utf-8')
text = text.replace('from agent.core import Agent\n', 'from agent.core import Agent, AgentConfig\n', 1)
old = 'def test_agent_invalidation_forces_refresh_on_live_repo_map(tmp_path):\n    (tmp_path / "first.py").write_text("def first(): pass\\n")\n    agent = Agent(MockBackend([]), ToolRegistry())\n'
new = 'def test_agent_invalidation_forces_refresh_on_live_repo_map(tmp_path):\n    (tmp_path / "first.py").write_text("def first(): pass\\n")\n    agent = Agent(MockBackend([]), ToolRegistry(), AgentConfig(repo_map_mode="query_aware"))\n'
if old not in text:
    raise RuntimeError('legacy refresh test anchor not found')
text = text.replace(old, new, 1)
text = text.replace(
    'def test_successful_file_write_refreshes_repo_map_before_next_step',
    'def test_successful_file_write_updates_repo_map_before_next_step',
    1,
)
path.write_text(text, encoding='utf-8')

print('P2 Repo Map core transformations applied')
