from context.repo_map import RepoMap
from context.incremental_repo_map import PersistentRepoMap


class CharacterCounter:
    name = "character-fixture"
    estimated = True

    def count_text(self, text):
        return len(text)


def test_strict_map_counter_includes_separators_and_omission_marker(tmp_path):
    (tmp_path / "target.py").write_text("def target_symbol():\n    pass\n")
    for index in range(8):
        (tmp_path / f"other{index}.py").write_text("def unrelated():\n    pass\n")
    repo_map = RepoMap(tmp_path)
    for budget in (0, 1, 40, 80, 160):
        result = repo_map.build(budget, query="target_symbol", counter=CharacterCounter())
        assert len(result) <= budget
    assert "target_symbol" in repo_map.build(80, query="target_symbol", counter=CharacterCounter())


def test_strict_persistent_map_remains_fresh_after_edit_and_delete(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "target.py"
    source.write_text("def old_symbol():\n    pass\n")
    repo_map = PersistentRepoMap(repo, cache_dir=tmp_path / "cache")
    assert "old_symbol" in repo_map.build(200, counter=CharacterCounter())
    source.write_text("def new_symbol():\n    pass\n")
    repo_map.update_paths(["target.py"])
    result = repo_map.build(200, counter=CharacterCounter())
    assert "new_symbol" in result and "old_symbol" not in result
    source.unlink()
    repo_map.update_paths(["target.py"])
    assert "new_symbol" not in repo_map.build(200, counter=CharacterCounter())
