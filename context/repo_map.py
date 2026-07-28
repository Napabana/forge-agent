"""Repository structure summarization for the agent context.

Scanning is split into file discovery, language preloading, and parsing.  A scan
therefore uses one immutable language snapshot per extension.
"""

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_LANG_REGISTRY: dict[str, tuple[str, str]] = {
    ".py": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".c": ("tree_sitter_c", "language"),
    ".rb": ("tree_sitter_ruby", "language"),
}
_FUNC_NODES = frozenset({
    "function_definition", "async_function_definition", "function_declaration",
    "method_declaration", "method_definition", "function_item", "arrow_function",
})
_CLASS_NODES = frozenset({
    "class_definition", "class_declaration", "struct_item", "impl_item",
    "interface_declaration",
})
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
    ".pytest_cache", "dist", "build",
})
_SYMBOL_RE = re.compile(
    r"^[ \t]*(def|class|function|func|fn|pub fn|async fn|async def"
    r"|public|private|protected|static)\s+(\w+)",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\b|import\s+\S+|"
    r"(?:const|let|var)\s+\w+\s*=\s*require\s*\(|"
    r"use\s+\S+|#include\s*[<\"]|package\s+\S+)",
    re.MULTILINE,
)
_MAX_FILE_SIZE = 500_000

# Only successful language loads are cached. Optional package failures are
# retried on later scans, so installing a package never requires a restart.
_lang_cache: dict[str, object] = {}
_UNSET = object()


@dataclass(frozen=True)
class LanguageLoadReport:
    """Result of preloading one language for a scan."""

    extension: str
    module: str
    available: bool
    from_cache: bool = False
    error: str | None = None


@dataclass(frozen=True)
class RepoMapScanReport:
    """Structured diagnostics for one complete repository scan."""

    root: Path
    force_refresh: bool
    files_discovered: int
    files_parsed: int
    languages: dict[str, LanguageLoadReport]
    file_errors: tuple[str, ...] = ()


def _load_language(
    ext: str,
    *,
    force_refresh: bool = False,
) -> tuple[object | None, LanguageLoadReport]:
    """Load one optional tree-sitter language and return diagnostics."""

    entry = _LANG_REGISTRY.get(ext)
    if entry is None:
        return None, LanguageLoadReport(ext, "", False, error="unsupported extension")

    module_name, attr_name = entry
    if force_refresh:
        _lang_cache.pop(ext, None)
    elif ext in _lang_cache:
        return _lang_cache[ext], LanguageLoadReport(
            ext, module_name, True, from_cache=True,
        )

    try:
        from tree_sitter import Language

        module = importlib.import_module(module_name)
        language = Language(getattr(module, attr_name)())
    except Exception as exc:
        # Deliberately do not cache None (no permanent negative cache).
        return None, LanguageLoadReport(
            ext,
            module_name,
            False,
            error=f"{type(exc).__name__}: {exc}",
        )

    _lang_cache[ext] = language
    return language, LanguageLoadReport(ext, module_name, True)


def _get_language(ext: str):
    """Compatibility helper returning only the language object."""

    language, _ = _load_language(ext)
    return language


def _preload_languages(
    extensions: Iterable[str],
    *,
    force_refresh: bool = False,
) -> tuple[dict[str, object | None], dict[str, LanguageLoadReport]]:
    """Create the fixed parser snapshot used by one complete scan."""

    snapshot: dict[str, object | None] = {}
    reports: dict[str, LanguageLoadReport] = {}
    for ext in sorted(set(extensions)):
        language, report = _load_language(ext, force_refresh=force_refresh)
        snapshot[ext] = language
        reports[ext] = report
    return snapshot, reports


@dataclass
class Symbol:
    """A function, class, or method definition extracted from source."""

    name: str
    kind: str
    line: int
    file: Path
    indent: int = 0

    @property
    def is_toplevel(self) -> bool:
        return self.indent == 0


@dataclass
class FileInfo:
    """Metadata and context-ranking signals for one repository file."""

    path: Path
    size: int
    symbols: list[Symbol] = field(default_factory=list)
    import_count: int = 0
    reference_count: int = 0
    path_score: float = 0.0
    _content: str = field(default="", repr=False, compare=False)

    @property
    def rel_path(self) -> str:
        return str(self.path)

    def importance_score(self) -> float:
        """Combine symbol, dependency, usage, path, and size signals."""

        symbol_score = sum(
            3.0 if symbol.kind == "class"
            else 2.0 if symbol.is_toplevel
            else 0.5
            for symbol in self.symbols
        )
        import_score = min(self.import_count, 20) * 0.12
        reference_score = math.log2(1 + self.reference_count) * 0.8
        size_penalty = math.log2(1 + self.size / 2_000) * 0.35
        return (
            symbol_score + import_score + reference_score
            + self.path_score - size_penalty
        )


class RepoMap:
    """Scan a repository and build an importance-ranked summary."""

    def __init__(self, repo_path: str | Path) -> None:
        self._root = Path(repo_path).resolve()
        self._files: list[FileInfo] | None = None
        self._last_report: RepoMapScanReport | None = None

    @property
    def last_report(self) -> RepoMapScanReport | None:
        return self._last_report

    def refresh(self) -> RepoMapScanReport:
        """Force language reload and a full discovery/parse pass."""

        self._files, self._last_report = self._scan(force_refresh=True)
        return self._last_report

    def build(self, budget: int = 8000, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.refresh()
        elif self._files is None:
            self._files, self._last_report = self._scan()

        files = list(self._files or ())
        if not files:
            return "(empty repository)"

        files.sort(key=lambda item: (-item.importance_score(), item.rel_path))
        lines: list[str] = []
        char_count = 0
        max_chars = budget * 4
        for index, file_info in enumerate(files):
            block = self._format_file(file_info)
            if char_count + len(block) > max_chars:
                lines.append(f"... ({len(files) - index} more files not shown)")
                break
            lines.append(block)
            char_count += len(block)
        return "\n".join(lines)

    def _discover_files(self) -> tuple[list[tuple[Path, Path, int]], list[str]]:
        """Discover files without loading languages or reading source."""

        discovered: list[tuple[Path, Path, int]] = []
        errors: list[str] = []
        if not self._root.is_dir():
            return discovered, errors
        try:
            candidates = sorted(self._root.rglob("*"))
        except OSError as exc:
            return discovered, [f"{self._root}: {exc}"]

        for path in candidates:
            relative_path = path.relative_to(self._root)
            if any(part in _SKIP_DIRS for part in relative_path.parts):
                continue
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if size <= _MAX_FILE_SIZE:
                discovered.append((path, relative_path, size))
        return discovered, errors

    def _scan(
        self,
        *,
        force_refresh: bool = False,
    ) -> tuple[list[FileInfo], RepoMapScanReport]:
        """Run discovery, language preload, and parsing as separate phases."""

        discovered, errors = self._discover_files()
        source_extensions = {
            path.suffix.lower()
            for path, _, _ in discovered
            if path.suffix.lower() in _LANG_REGISTRY
        }
        language_snapshot, language_reports = _preload_languages(
            source_extensions,
            force_refresh=force_refresh,
        )

        results: list[FileInfo] = []
        parsed = 0
        for path, relative_path, size in discovered:
            file_info = FileInfo(
                path=relative_path,
                size=size,
                path_score=_path_importance(relative_path),
            )
            ext = path.suffix.lower()
            if ext in language_snapshot:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
                else:
                    file_info._content = content
                    file_info.import_count = len(_IMPORT_RE.findall(content))
                    file_info.symbols = _extract_symbols(
                        content,
                        relative_path,
                        ext,
                        language=language_snapshot[ext],
                    )
                    parsed += 1
            results.append(file_info)

        _apply_reference_scores(results)
        report = RepoMapScanReport(
            root=self._root,
            force_refresh=force_refresh,
            files_discovered=len(discovered),
            files_parsed=parsed,
            languages=language_reports,
            file_errors=tuple(errors),
        )
        return results, report

    def _format_file(self, file_info: FileInfo) -> str:
        symbol_count = len(file_info.symbols)
        header = file_info.rel_path
        if symbol_count:
            suffix = "s" if symbol_count != 1 else ""
            header += f" ({symbol_count} symbol{suffix})"
        if not file_info.symbols:
            return header + "\n"

        symbol_lines = [header + ":"]
        for symbol in file_info.symbols:
            prefix = "    " if not symbol.is_toplevel else "  "
            symbol_lines.append(
                f"{prefix}{symbol.kind} {symbol.name} (line {symbol.line})"
            )
        return "\n".join(symbol_lines) + "\n"


def _path_importance(path: Path) -> float:
    """Give conventional entry points and core source a bounded boost."""

    parts = tuple(part.lower() for part in path.parts)
    stem = path.stem.lower()
    score = 0.0
    if stem in {"main", "app", "api", "core", "cli", "index", "__init__"}:
        score += 1.2
    if any(part in {"src", "lib", "agent", "core"} for part in parts[:-1]):
        score += 0.5
    if any(
        part in {"test", "tests", "docs", "examples", "fixtures"}
        for part in parts[:-1]
    ):
        score -= 0.8
    return score


def _apply_reference_scores(files: list[FileInfo]) -> None:
    """Count cross-file uses of definitions as a centrality signal."""

    owners: dict[str, set[Path]] = {}
    files_by_path = {file_info.path: file_info for file_info in files}
    for file_info in files:
        for symbol in file_info.symbols:
            if len(symbol.name) >= 3:
                owners.setdefault(symbol.name, set()).add(file_info.path)

    for file_info in files:
        if not file_info._content:
            continue
        for name, defining_files in owners.items():
            if file_info.path in defining_files:
                continue
            occurrences = len(re.findall(rf"\b{re.escape(name)}\b", file_info._content))
            for defining_path in defining_files:
                files_by_path[defining_path].reference_count += occurrences


def _extract_symbols(
    content: str,
    filepath: Path,
    ext: str,
    *,
    language: object = _UNSET,
) -> list[Symbol]:
    """Extract symbols using a scan snapshot or a one-off lookup."""

    selected = _get_language(ext) if language is _UNSET else language
    if selected is not None:
        return _extract_with_treesitter(content, filepath, selected)
    return _extract_symbols_regex(content, filepath)


def _extract_with_treesitter(
    content: str,
    filepath: Path,
    language: object,
) -> list[Symbol]:
    """Extract with tree-sitter, falling back to regex on parser failure."""

    try:
        from tree_sitter import Parser

        parser = Parser(language)
        tree = parser.parse(content.encode("utf-8", errors="replace"))
        return _walk_tree(tree.root_node, filepath)
    except Exception:
        return _extract_symbols_regex(content, filepath)


def _walk_tree(node, filepath: Path, *, class_ancestor: bool = False) -> list[Symbol]:
    """Classify methods by AST ancestry rather than indentation."""

    results: list[Symbol] = []
    node_type = node.type
    is_class = node_type in _CLASS_NODES
    if node_type in _FUNC_NODES and node_type != "arrow_function":
        name_node = node.child_by_field_name("name")
        if name_node:
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind="method" if class_ancestor else "function",
                line=node.start_point[0] + 1,
                file=filepath,
                indent=node.start_point[1],
            ))
    elif is_class:
        name_node = node.child_by_field_name("name")
        if name_node:
            results.append(Symbol(
                name=name_node.text.decode("utf-8", errors="replace"),
                kind="class",
                line=node.start_point[0] + 1,
                file=filepath,
                indent=node.start_point[1],
            ))

    child_has_class_ancestor = class_ancestor or is_class
    for child in node.children:
        results.extend(_walk_tree(
            child,
            filepath,
            class_ancestor=child_has_class_ancestor,
        ))
    return results


def _extract_python_symbols(content: str, filepath: Path) -> list[Symbol]:
    """Compatibility wrapper retained for existing callers and tests."""

    return _extract_symbols(content, filepath, ".py")


def _extract_symbols_regex(content: str, filepath: Path) -> list[Symbol]:
    """Best-effort multi-language fallback."""

    symbols: list[Symbol] = []
    python_class_indents: list[int] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = _SYMBOL_RE.match(line)
        if not match:
            continue
        keyword, name = match.groups()
        if keyword in ("public", "private", "protected", "static"):
            continue

        indent = len(line) - len(line.lstrip())
        while python_class_indents and indent <= python_class_indents[-1]:
            python_class_indents.pop()
        if keyword == "class":
            kind = "class"
            python_class_indents.append(indent)
        elif python_class_indents:
            kind = "method"
        else:
            kind = "function"
        symbols.append(Symbol(
            name=name,
            kind=kind,
            line=line_number,
            file=filepath,
            indent=indent,
        ))
    return symbols