from pathlib import Path


def replace_once(path: Path, old: str, new: str, marker: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0 and marker in text:
        print(f"{label} already applied")
        return
    if count != 1:
        raise RuntimeError(f"expected one {label} anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"{label} applied")


replace_once(
    Path("agent/runner.py"),
    (
        '        token_budget = TokenBudget(total=config.budget_tokens)\n'
        '        repo_map = getattr(self.agent, "_repo_map_instance", self.agent._new_repo_map(task.repo_path))\n'
        '        return self.agent._prepare_next_turn(\n'
    ),
    (
        '        token_budget = TokenBudget(total=config.budget_tokens)\n'
        '        repo_map = getattr(self.agent, "_repo_map_instance", None)\n'
        '        if repo_map is None:\n'
        '            repo_map = self.agent._new_repo_map(task.repo_path)\n'
        '            self.agent._repo_map_instance = repo_map\n'
        '        return self.agent._prepare_next_turn(\n'
    ),
    'repo_map = getattr(self.agent, "_repo_map_instance", None)',
    "runner Repo Map reuse transform",
)

replace_once(
    Path("context/repo_index.py"),
    (
        '                "SELECT owners.file_path, SUM(symbol_references.occurrences) "\n'
        '                "FROM owners JOIN symbol_references "\n'
        '                "ON symbol_references.referenced_symbol = owners.name "\n'
        '                "AND symbol_references.source_file <> owners.file_path "\n'
        '                "GROUP BY owners.file_path"\n'
    ),
    (
        '                "SELECT owners.file_path, SUM(symbol_references.occurrences) "\n'
        '                "FROM owners JOIN symbol_references "\n'
        '                "ON symbol_references.referenced_symbol = owners.name "\n'
        '                "WHERE NOT EXISTS ("\n'
        '                "  SELECT 1 FROM symbols AS source_definition "\n'
        '                "  WHERE source_definition.file_path = symbol_references.source_file "\n'
        '                "  AND source_definition.name = owners.name"\n'
        '                ") "\n'
        '                "GROUP BY owners.file_path"\n'
    ),
    '"WHERE NOT EXISTS ("',
    "reference semantics transform",
)
