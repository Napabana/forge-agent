"""Persistent, incrementally updated Repo Map implementation."""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from context.repo_index import PersistentRepoIndex, StoredFile, StoredSymbol
from context.repo_map import (
    FileInfo,
    LanguageLoadReport,
    RepoMap,
    RepoMapScanReport,
    Symbol,
    _IDENTIFIER_RE,
    _IMPORT_RE,
    _LANG_REGISTRY,
    _MAX_FILE_SIZE,
    _SKIP_DIRS,
    _extract_symbols,
    _path_importance,
    _preload_languages,
)
from context.repository_state import (
    RepositoryState,
    capture_repository_state,
    detect_repository_changes,
)


@dataclass
class RepoMapIndexMetrics:
    full_rebuild_seconds: list[float] = field(default_factory=list)
    warm_load_seconds: list[float] = field(default_factory=list)
    incremental_update_seconds: list[float] = field(default_factory=list)
    parsed_paths: list[str] = field(default_factory=list)
    last_operation: str = ""
    fallback_reasons: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, object]:
        return {
            "full_rebuild_seconds": list(self.full_rebuild_seconds),
            "warm_load_seconds": list(self.warm_load_seconds),
            "incremental_update_seconds": list(self.incremental_update_seconds),
            "parsed_paths": list(self.parsed_paths),
            "last_operation": self.last_operation,
            "fallback_reasons": list(self.fallback_reasons),
        }


class PersistentRepoMap(RepoMap):
    """Repo Map whose structural scan is backed by a versioned SQLite index."""

    def __init__(
        self,
        repo_path: str | Path,
        *,
        index_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        super().__init__(repo_path)
        self._index = PersistentRepoIndex(
            self._root,
            index_path=index_path,
            cache_dir=cache_dir,
        )
        self.metrics = RepoMapIndexMetrics()
        if self._index.reset_reason:
            self.metrics.fallback_reasons.append(self._index.reset_reason)

    @property
    def index_path(self) -> Path:
        return self._index.path

    def build(
        self,
        budget: int = 8000,
        *,
        force_refresh: bool = False,
        query: str | None = None,
    ) -> str:
        if force_refresh:
            self.refresh()
        elif self._files is None:
            self.sync()
        return super().build(budget=budget, query=query)

    def refresh(self) -> RepoMapScanReport:
        """Explicit full rebuild fallback, preserving RepoMap.refresh semantics."""
        return self._full_rebuild(force_refresh=True, reason="explicit_refresh")

    def sync(self) -> RepoMapScanReport:
        """Load a warm index and apply only repository changes when reliable."""
        if not self._index.ready:
            reason = self._index.reset_reason or "missing_persistent_index"
            return self._full_rebuild(reason=reason)

        started = time.perf_counter()
        try:
            previous = RepositoryState.from_json(self._index.repository_state_json())
            current = capture_repository_state(self._root)
            files = self._load_files_from_index()
            changes = detect_repository_changes(self._root, previous, current)
        except Exception as exc:
            self.metrics.fallback_reasons.append(f"load_or_detect:{type(exc).__name__}")
            return self._full_rebuild(reason=f"load_or_detect:{type(exc).__name__}")

        self._files = files
        if changes.full_rebuild:
            self.metrics.fallback_reasons.append(changes.reason)
            return self._full_rebuild(reason=changes.reason)
        if changes.all_paths:
            return self.update_paths(changes.all_paths, current_state=current)

        self._index.update_repository_state(current.to_json())
        elapsed = time.perf_counter() - started
        self.metrics.warm_load_seconds.append(elapsed)
        self.metrics.last_operation = "warm_load"
        self._last_report = RepoMapScanReport(
            root=self._root,
            force_refresh=False,
            files_discovered=len(files),
            files_parsed=0,
            languages={},
            file_errors=(),
        )
        return self._last_report

    def update_paths(
        self,
        paths: Iterable[str | Path],
        *,
        current_state: RepositoryState | None = None,
    ) -> RepoMapScanReport:
        """Reparse only named paths, then resolve references from persisted tokens."""
        if not self._index.ready:
            return self._full_rebuild(reason="incremental_update_without_ready_index")

        started = time.perf_counter()
        changes: dict[str, StoredFile | None] = {}
        language_reports: dict[str, LanguageLoadReport] = {}
        errors: list[str] = []
        parsed = 0
        normalized = self._normalize_paths(paths)
        for relative in normalized:
            stored, report, error = self._parse_path(relative)
            changes[relative.as_posix()] = stored
            if report is not None:
                language_reports[report.extension] = report
            if error:
                errors.append(error)
            if stored is not None and stored.language in _LANG_REGISTRY:
                parsed += 1
                self.metrics.parsed_paths.append(relative.as_posix())

        try:
            state = current_state or capture_repository_state(self._root)
            self._index.apply_changes(changes, state.to_json())
            self._files = self._load_files_from_index()
        except Exception as exc:
            self.metrics.fallback_reasons.append(f"incremental_apply:{type(exc).__name__}")
            return self._full_rebuild(reason=f"incremental_apply:{type(exc).__name__}")

        elapsed = time.perf_counter() - started
        self.metrics.incremental_update_seconds.append(elapsed)
        self.metrics.last_operation = "incremental_update"
        self._last_report = RepoMapScanReport(
            root=self._root,
            force_refresh=False,
            files_discovered=len(self._files),
            files_parsed=parsed,
            languages=language_reports,
            file_errors=tuple(errors),
        )
        return self._last_report

    def _full_rebuild(
        self,
        *,
        force_refresh: bool = False,
        reason: str = "full_rebuild",
    ) -> RepoMapScanReport:
        started = time.perf_counter()
        files, report = super()._scan(force_refresh=force_refresh)
        try:
            state = capture_repository_state(self._root)
            stored = [self._stored_file(item) for item in files]
            self._index.replace_all(stored, state.to_json())
            self._files = self._load_files_from_index()
        except Exception as exc:
            self.metrics.fallback_reasons.append(f"persist:{type(exc).__name__}")
            self._files = files
        elapsed = time.perf_counter() - started
        self.metrics.full_rebuild_seconds.append(elapsed)
        self.metrics.last_operation = "full_rebuild"
        if reason and reason not in {"full_rebuild", "missing_persistent_index"}:
            self.metrics.fallback_reasons.append(reason)
        self._last_report = report
        return report

    def _load_files_from_index(self) -> list[FileInfo]:
        files: list[FileInfo] = []
        for stored in self._index.load_files():
            path = Path(stored.path)
            files.append(FileInfo(
                path=path,
                size=stored.size,
                symbols=[
                    Symbol(
                        name=symbol.name,
                        kind=symbol.kind,
                        line=symbol.line,
                        file=path,
                        indent=symbol.indent,
                    )
                    for symbol in stored.symbols
                ],
                import_count=stored.import_count,
                reference_count=stored.reference_count,
                path_score=stored.path_score,
                _content=stored.content,
            ))
        return files

    def _stored_file(self, file_info: FileInfo) -> StoredFile:
        absolute = self._root / file_info.path
        try:
            stat = absolute.stat()
            raw = absolute.read_bytes()
        except OSError:
            stat = None
            raw = file_info._content.encode("utf-8", errors="replace")
        content_hash = hashlib.sha256(raw).hexdigest()
        ext = file_info.path.suffix.lower()
        content = file_info._content if ext in _LANG_REGISTRY else ""
        return StoredFile(
            path=file_info.path.as_posix(),
            content_hash=content_hash,
            mtime_ns=int(stat.st_mtime_ns) if stat is not None else 0,
            size=file_info.size,
            language=ext,
            import_count=file_info.import_count,
            path_score=file_info.path_score,
            content=content,
            symbols=tuple(
                StoredSymbol(
                    name=symbol.name,
                    kind=symbol.kind,
                    line=symbol.line,
                    indent=symbol.indent,
                )
                for symbol in file_info.symbols
            ),
            imports=tuple(_extract_import_targets(content)),
            identifier_counts=Counter(_IDENTIFIER_RE.findall(content)),
        )

    def _parse_path(
        self,
        relative: Path,
    ) -> tuple[StoredFile | None, LanguageLoadReport | None, str | None]:
        if any(part in _SKIP_DIRS for part in relative.parts):
            return None, None, None
        absolute = self._root / relative
        try:
            if not absolute.is_file():
                return None, None, None
            stat = absolute.stat()
        except OSError as exc:
            return None, None, f"{absolute}: {exc}"
        if stat.st_size > _MAX_FILE_SIZE:
            return None, None, None

        file_info = FileInfo(
            path=relative,
            size=stat.st_size,
            path_score=_path_importance(relative),
        )
        ext = relative.suffix.lower()
        language_report: LanguageLoadReport | None = None
        if ext in _LANG_REGISTRY:
            snapshot, reports = _preload_languages({ext})
            language_report = reports[ext]
            try:
                content = absolute.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return None, language_report, f"{absolute}: {exc}"
            file_info._content = content
            file_info.import_count = len(_IMPORT_RE.findall(content))
            file_info.symbols = _extract_symbols(
                content,
                relative,
                ext,
                language=snapshot[ext],
            )
        return self._stored_file(file_info), language_report, None

    def _normalize_paths(self, paths: Iterable[str | Path]) -> list[Path]:
        result: set[Path] = set()
        for raw in paths:
            if raw is None or str(raw) == "":
                continue
            path = Path(raw)
            absolute = path.resolve() if path.is_absolute() else (self._root / path).resolve()
            try:
                relative = absolute.relative_to(self._root)
            except ValueError:
                continue
            result.add(relative)
        return sorted(result, key=lambda item: item.as_posix())


_PY_FROM_RE = re.compile(r"^\s*from\s+([^\s]+)\s+import\b", re.MULTILINE)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([^\n#]+)", re.MULTILINE)
_REQUIRE_RE = re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_JS_FROM_RE = re.compile(r"\bfrom\s+['\"]([^'\"]+)['\"]")
_RUST_USE_RE = re.compile(r"^\s*use\s+([^;\s]+)", re.MULTILINE)
_INCLUDE_RE = re.compile(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE)
_PACKAGE_RE = re.compile(r"^\s*package\s+([^;\s]+)", re.MULTILINE)


def _extract_import_targets(content: str) -> list[str]:
    """Persist lightweight import/module relations without changing rank semantics."""
    targets: list[str] = []
    targets.extend(match.group(1) for match in _PY_FROM_RE.finditer(content))
    for match in _PY_IMPORT_RE.finditer(content):
        for item in match.group(1).split(","):
            module = item.strip().split()[0] if item.strip() else ""
            if module:
                targets.append(module)
    for regex in (_REQUIRE_RE, _JS_FROM_RE, _RUST_USE_RE, _INCLUDE_RE, _PACKAGE_RE):
        targets.extend(match.group(1) for match in regex.finditer(content))
    return targets
