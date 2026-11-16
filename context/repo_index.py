"""Persistent SQLite storage for Repo Map structural data.

The index lives outside the user repository by default. It stores only signals
already used by Repo Map ranking plus repository-state metadata; query ranking
remains a separate in-memory/rendering concern.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StoredSymbol:
    name: str
    kind: str
    line: int
    indent: int = 0


@dataclass(frozen=True)
class StoredFile:
    path: str
    content_hash: str
    mtime_ns: int
    size: int
    language: str
    import_count: int
    path_score: float
    content: str = ""
    symbols: tuple[StoredSymbol, ...] = ()
    imports: tuple[str, ...] = ()
    identifier_counts: Mapping[str, int] = field(default_factory=dict)
    reference_count: int = 0


def default_index_path(repo_root: str | Path, cache_dir: str | Path | None = None) -> Path:
    root = Path(repo_root).resolve()
    if cache_dir is None:
        base = os.environ.get("FORGE_AGENT_CACHE_DIR") or os.environ.get("XDG_CACHE_HOME")
        if base:
            cache_root = Path(base).expanduser()
        else:
            cache_root = Path.home() / ".cache"
        cache_root = cache_root / "forge-agent"
    else:
        cache_root = Path(cache_dir).expanduser().resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in root.name) or "repo"
    return cache_root / "repo-map" / f"{stem}-{digest}.sqlite3"


class PersistentRepoIndex:
    """Small versioned SQLite index with safe rebuild fallback semantics."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        index_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.path = Path(index_path).resolve() if index_path else default_index_path(self.repo_root, cache_dir)
        self.reset_reason: str | None = None
        self._conn: sqlite3.Connection | None = None
        self._open()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "PersistentRepoIndex":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def ready(self) -> bool:
        return self.get_metadata("ready") == "1"

    def get_metadata(self, key: str) -> str | None:
        row = self._execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self._execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def repository_state_json(self) -> str | None:
        return self.get_metadata("repository_state")

    def load_files(self) -> list[StoredFile]:
        conn = self._require_conn()
        symbols: dict[str, list[StoredSymbol]] = {}
        for row in conn.execute(
            "SELECT file_path, name, kind, line, indent FROM symbols ORDER BY file_path, ordinal"
        ):
            symbols.setdefault(row[0], []).append(
                StoredSymbol(name=row[1], kind=row[2], line=int(row[3]), indent=int(row[4]))
            )

        imports: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT source_file, target_module FROM imports ORDER BY source_file, ordinal"
        ):
            imports.setdefault(row[0], []).append(row[1])

        identifiers: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT source_file, referenced_symbol, occurrences "
            "FROM symbol_references ORDER BY source_file, referenced_symbol"
        ):
            identifiers.setdefault(row[0], {})[row[1]] = int(row[2])

        reference_counts = {
            row[0]: int(row[1] or 0)
            for row in conn.execute(
                "WITH owners AS ("
                "  SELECT DISTINCT file_path, name FROM symbols WHERE length(name) >= 3"
                ") "
                "SELECT owners.file_path, SUM(symbol_references.occurrences) "
                "FROM owners JOIN symbol_references "
                "ON symbol_references.referenced_symbol = owners.name "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM symbols AS source_definition "
                "  WHERE source_definition.file_path = symbol_references.source_file "
                "  AND source_definition.name = owners.name"
                ") "
                "GROUP BY owners.file_path"
            )
        }

        result: list[StoredFile] = []
        for row in conn.execute(
            "SELECT path, content_hash, mtime_ns, size, language, import_count, path_score, content "
            "FROM files ORDER BY path"
        ):
            path = row[0]
            result.append(StoredFile(
                path=path,
                content_hash=row[1],
                mtime_ns=int(row[2]),
                size=int(row[3]),
                language=row[4],
                import_count=int(row[5]),
                path_score=float(row[6]),
                content=row[7] or "",
                symbols=tuple(symbols.get(path, ())),
                imports=tuple(imports.get(path, ())),
                identifier_counts=identifiers.get(path, {}),
                reference_count=reference_counts.get(path, 0),
            ))
        return result

    def replace_all(self, files: Sequence[StoredFile], repository_state_json: str) -> None:
        conn = self._require_conn()
        with conn:
            conn.execute("DELETE FROM symbol_references")
            conn.execute("DELETE FROM imports")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM files")
            for file_info in files:
                self._insert_file(conn, file_info)
            self._set_metadata_tx(conn, "schema_version", str(SCHEMA_VERSION))
            self._set_metadata_tx(conn, "repo_root", str(self.repo_root))
            self._set_metadata_tx(conn, "repository_state", repository_state_json)
            self._set_metadata_tx(conn, "ready", "1")

    def apply_changes(
        self,
        changed: Mapping[str, StoredFile | None],
        repository_state_json: str,
    ) -> None:
        conn = self._require_conn()
        with conn:
            for path, file_info in changed.items():
                self._delete_file_tx(conn, path)
                if file_info is not None:
                    self._insert_file(conn, file_info)
            self._set_metadata_tx(conn, "repository_state", repository_state_json)
            self._set_metadata_tx(conn, "ready", "1")

    def update_repository_state(self, repository_state_json: str) -> None:
        conn = self._require_conn()
        with conn:
            self._set_metadata_tx(conn, "repository_state", repository_state_json)

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=10.0)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            row = self._conn.execute("PRAGMA quick_check").fetchone()
            if row and row[0] != "ok":
                raise sqlite3.DatabaseError(f"quick_check failed: {row[0]}")
            self._create_schema()
            version = self.get_metadata("schema_version")
            root = self.get_metadata("repo_root")
            if version is not None and version != str(SCHEMA_VERSION):
                self._reset("schema_version_mismatch")
            elif root is not None and Path(root).resolve() != self.repo_root:
                self._reset("repo_root_mismatch")
        except (OSError, sqlite3.DatabaseError, ValueError) as exc:
            self._reset(f"corrupted_or_unusable:{type(exc).__name__}")

    def _reset(self, reason: str) -> None:
        self.reset_reason = reason
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        conn = self._require_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                language TEXT NOT NULL,
                import_count INTEGER NOT NULL,
                path_score REAL NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS symbols (
                file_path TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                line INTEGER NOT NULL,
                indent INTEGER NOT NULL,
                PRIMARY KEY(file_path, ordinal),
                FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE TABLE IF NOT EXISTS imports (
                source_file TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                target_module TEXT NOT NULL,
                PRIMARY KEY(source_file, ordinal),
                FOREIGN KEY(source_file) REFERENCES files(path) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS symbol_references (
                source_file TEXT NOT NULL,
                referenced_symbol TEXT NOT NULL,
                occurrences INTEGER NOT NULL,
                PRIMARY KEY(source_file, referenced_symbol),
                FOREIGN KEY(source_file) REFERENCES files(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_symbol_references_symbol
                ON symbol_references(referenced_symbol);
            """
        )
        conn.commit()

    @staticmethod
    def _set_metadata_tx(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @staticmethod
    def _delete_file_tx(conn: sqlite3.Connection, path: str) -> None:
        conn.execute("DELETE FROM symbol_references WHERE source_file = ?", (path,))
        conn.execute("DELETE FROM imports WHERE source_file = ?", (path,))
        conn.execute("DELETE FROM symbols WHERE file_path = ?", (path,))
        conn.execute("DELETE FROM files WHERE path = ?", (path,))

    @staticmethod
    def _insert_file(conn: sqlite3.Connection, file_info: StoredFile) -> None:
        conn.execute(
            "INSERT INTO files(path, content_hash, mtime_ns, size, language, import_count, path_score, content) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_info.path,
                file_info.content_hash,
                int(file_info.mtime_ns),
                int(file_info.size),
                file_info.language,
                int(file_info.import_count),
                float(file_info.path_score),
                file_info.content,
            ),
        )
        conn.executemany(
            "INSERT INTO symbols(file_path, ordinal, name, kind, line, indent) VALUES(?, ?, ?, ?, ?, ?)",
            [
                (file_info.path, i, symbol.name, symbol.kind, int(symbol.line), int(symbol.indent))
                for i, symbol in enumerate(file_info.symbols)
            ],
        )
        conn.executemany(
            "INSERT INTO imports(source_file, ordinal, target_module) VALUES(?, ?, ?)",
            [(file_info.path, i, target) for i, target in enumerate(file_info.imports)],
        )
        conn.executemany(
            "INSERT INTO symbol_references(source_file, referenced_symbol, occurrences) VALUES(?, ?, ?)",
            [
                (file_info.path, name, int(count))
                for name, count in sorted(file_info.identifier_counts.items())
                if len(name) >= 3 and count > 0
            ],
        )

    def _execute(self, sql: str, params: Iterable[object] = ()) -> sqlite3.Cursor:
        try:
            return self._require_conn().execute(sql, tuple(params))
        except sqlite3.DatabaseError:
            self._reset("runtime_database_error")
            return self._require_conn().execute(sql, tuple(params))

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("persistent Repo Map index is closed")
        return self._conn
