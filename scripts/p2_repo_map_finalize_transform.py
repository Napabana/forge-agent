from pathlib import Path

path = Path("agent/runner.py")
text = path.read_text(encoding="utf-8")
old = (
    '        token_budget = TokenBudget(total=config.budget_tokens)\n'
    '        repo_map = getattr(self.agent, "_repo_map_instance", self.agent._new_repo_map(task.repo_path))\n'
    '        return self.agent._prepare_next_turn(\n'
)
new = (
    '        token_budget = TokenBudget(total=config.budget_tokens)\n'
    '        repo_map = getattr(self.agent, "_repo_map_instance", None)\n'
    '        if repo_map is None:\n'
    '            repo_map = self.agent._new_repo_map(task.repo_path)\n'
    '            self.agent._repo_map_instance = repo_map\n'
    '        return self.agent._prepare_next_turn(\n'
)
count = text.count(old)
if count == 0 and 'repo_map = getattr(self.agent, "_repo_map_instance", None)' in text:
    print("P2 final runner transform already applied")
elif count != 1:
    raise RuntimeError(f"expected one runner Repo Map anchor, found {count}")
else:
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("P2 final runner transform applied")
