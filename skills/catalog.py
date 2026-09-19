"""Filesystem discovery and safe parsing for Agent Skills."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_DESCRIPTION_CHARS = 500
_MAX_RESOURCE_FILES = 32


@dataclass(frozen=True)
class SkillDiscoveryIssue:
    directory: str
    source: str
    error: str

    def to_payload(self) -> dict[str, str]:
        return {
            "directory": self.directory,
            "source": self.source,
            "error": self.error,
        }


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    instructions: str
    root: Path
    source: str
    references: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()

    def metadata(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "reference_count": len(self.references),
            "script_count": len(self.scripts),
        }

    def render(self, *, max_chars: int) -> str:
        instructions = self.instructions.strip()
        if len(instructions) > max_chars:
            instructions = instructions[:max_chars].rstrip() + "\n[Skill instructions truncated]"
        lines = [
            f"### Loaded Skill: {self.name}",
            f"Description: {self.description}",
            "",
            instructions or "(No additional instructions.)",
        ]
        if self.references:
            lines.extend([
                "",
                "References available on demand via skill_reference_load:",
                *[f"- {path}" for path in self.references],
            ])
        if self.scripts:
            lines.extend([
                "",
                "Scripts declared by this skill:",
                *[f"- {path}" for path in self.scripts],
                (
                    "Scripts are never executed by the Skill subsystem. "
                    "Any executable action must use the normal Shell/ToolExecutor lifecycle."
                ),
            ])
        return "\n".join(lines)


class SkillCatalog:
    """Immutable catalog with project skills overriding same-name global skills."""

    def __init__(
        self,
        skills: Iterable[SkillDefinition] = (),
        issues: Iterable[SkillDiscoveryIssue] = (),
    ) -> None:
        self._skills = {skill.name: skill for skill in skills}
        self._issues = tuple(issues)

    @classmethod
    def discover(
        cls,
        repo_path: str | Path,
        *,
        global_root: str | Path | None = None,
    ) -> "SkillCatalog":
        merged: dict[str, SkillDefinition] = {}
        issues: list[SkillDiscoveryIssue] = []
        if global_root:
            global_skills, global_issues = _discover_root(
                Path(global_root).expanduser(), source="global"
            )
            issues.extend(global_issues)
            for skill in global_skills:
                merged[skill.name] = skill
        project_root = Path(repo_path).resolve() / ".agents" / "skills"
        project_skills, project_issues = _discover_root(
            project_root, source="project"
        )
        issues.extend(project_issues)
        for skill in project_skills:
            merged[skill.name] = skill
        return cls(merged.values(), issues)

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)

    @property
    def issues(self) -> tuple[SkillDiscoveryIssue, ...]:
        return self._issues

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def __len__(self) -> int:
        return len(self._skills)

    def read_reference(
        self,
        skill_name: str,
        reference: str,
        *,
        max_chars: int,
    ) -> str:
        skill = self.get(skill_name)
        if skill is None:
            raise ValueError(f"unknown skill: {skill_name}")
        normalized = reference.replace("\\", "/").strip("/")
        if normalized not in skill.references:
            raise ValueError(
                f"unknown reference {reference!r} for skill {skill_name!r}"
            )
        target = (skill.root / "references" / normalized).resolve()
        reference_root = (skill.root / "references").resolve()
        try:
            target.relative_to(reference_root)
        except ValueError as exc:
            raise ValueError("skill reference escaped its references directory") from exc
        if target.is_symlink() or not target.is_file():
            raise ValueError("skill reference is not a regular file")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("skill reference must be UTF-8 text") from exc
        if len(content) > max_chars:
            return content[:max_chars].rstrip() + "\n[Skill reference truncated]"
        return content


def _discover_root(
    root: Path,
    *,
    source: str,
) -> tuple[list[SkillDefinition], list[SkillDiscoveryIssue]]:
    if not root.is_dir():
        return [], []
    discovered: list[SkillDefinition] = []
    issues: list[SkillDiscoveryIssue] = []
    try:
        directories = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return [], [SkillDiscoveryIssue(root.name, source, f"{type(exc).__name__}: {exc}")]
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        skill_file = directory / "SKILL.md"
        if skill_file.is_symlink() or not skill_file.is_file():
            continue
        try:
            discovered.append(_load_skill(skill_file, source=source))
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
            issues.append(
                SkillDiscoveryIssue(
                    directory=directory.name,
                    source=source,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return discovered, issues


def _load_skill(path: Path, *, source: str) -> SkillDefinition:
    raw = path.read_text(encoding="utf-8")
    metadata, instructions = _parse_frontmatter(raw)
    name = str(metadata.get("name", "")).strip().lower()
    description = " ".join(str(metadata.get("description", "")).split())
    if not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid skill name: {name!r}")
    if name != path.parent.name:
        raise ValueError(
            f"skill name {name!r} must match directory name {path.parent.name!r}"
        )
    if not description:
        raise ValueError(f"skill {name!r} requires a description")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise ValueError(f"skill {name!r} description is too long")

    references = _resource_manifest(path.parent / "references")
    scripts = _resource_manifest(path.parent / "scripts")
    return SkillDefinition(
        name=name,
        description=description,
        instructions=instructions,
        root=path.parent.resolve(),
        source=source,
        references=references,
        scripts=scripts,
    )


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("SKILL.md frontmatter is not terminated") from exc
    parsed = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    return parsed, "\n".join(lines[end + 1:]).strip()


def _resource_manifest(root: Path) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        paths.append(relative)
        if len(paths) >= _MAX_RESOURCE_FILES:
            break
    return tuple(paths)
