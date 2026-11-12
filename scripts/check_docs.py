#!/usr/bin/env python3
"""Deterministic architecture-document impact and consistency checks."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


DEFAULT_MAP = Path("docs/design/architecture-map.json")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^)]*)?\)")
SOURCE_RE = re.compile(r"`((?:[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_.*?\[\]-]+)\.py)`")
SNIPPET_RE = re.compile(r'^\s*--8<--\s+"([^"]+)"\s*$', re.MULTILINE)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    old_path: str | None = None


def _run(command: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=check)


def load_mapping(repo_root: Path, mapping_path: Path = DEFAULT_MAP) -> dict:
    path = mapping_path if mapping_path.is_absolute() else repo_root / mapping_path
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_name_status(raw: str) -> list[ChangedPath]:
    tokens = raw.split("\0")
    changes: list[ChangedPath] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index]
        index += 1
        if status[0] in {"R", "C"}:
            old_path, new_path = tokens[index], tokens[index + 1]
            changes.append(ChangedPath(status, new_path, old_path))
            index += 2
        else:
            changes.append(ChangedPath(status, tokens[index]))
            index += 1
    return changes


def collect_git_changes(repo_root: Path, base: str = "HEAD") -> list[ChangedPath]:
    """Return BASE..HEAD plus staged, unstaged and untracked changes.

    This deliberately uses two-dot commit comparison. It does not use merge-base
    semantics and never changes the working tree.
    """

    _run(["git", "rev-parse", "--verify", f"{base}^{{commit}}"], repo_root)
    committed = _run(
        ["git", "diff", "--name-status", "-z", "--no-renames", f"{base}..HEAD"],
        repo_root,
    ).stdout
    working = _run(
        ["git", "diff", "--name-status", "-z", "--no-renames", "HEAD"],
        repo_root,
    ).stdout
    untracked = _run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], repo_root
    ).stdout

    ordered = _parse_name_status(committed) + _parse_name_status(working)
    ordered.extend(ChangedPath("A", path) for path in untracked.split("\0") if path)
    unique: dict[tuple[str, str | None], ChangedPath] = {}
    for change in ordered:
        unique[(change.path, change.old_path)] = change
    return list(unique.values())


def _matches(path: str, patterns: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def modules_for_path(path: str, mapping: dict) -> list[dict]:
    return [module for module in mapping["modules"] if _matches(path, module["source_patterns"])]


def _is_source(path: str, mapping: dict) -> bool:
    normalized = path.replace("\\", "/")
    if modules_for_path(normalized, mapping):
        return True
    parts = Path(normalized).parts
    return bool(parts and parts[0] in mapping["source_roots"] and normalized.endswith(".py"))


def analyze_changes(changes: list[ChangedPath], mapping: dict) -> dict:
    changed_paths = {change.path.replace("\\", "/") for change in changes}
    source_changes: list[ChangedPath] = []
    affected: dict[str, dict] = {}
    unmapped: set[str] = set()

    for change in changes:
        candidates = [change.path]
        if change.old_path:
            candidates.append(change.old_path)
        if not any(_is_source(path, mapping) for path in candidates):
            continue
        source_changes.append(change)
        matched = {
            module["id"]: module
            for path in candidates
            for module in modules_for_path(path, mapping)
        }
        if matched:
            affected.update(matched)
        else:
            unmapped.add(change.path.replace("\\", "/"))

    if not source_changes:
        status = "NO_IMPACT"
    elif unmapped:
        status = "UNMAPPED"
    else:
        expected_docs = {module["document"] for module in affected.values()}
        expected_docs.add(mapping["source_index"])
        status = "DOCS_UPDATED" if expected_docs <= changed_paths else "REVIEW_REQUIRED"

    related_docs = {mapping["source_index"]}
    for module in affected.values():
        related_docs.add(module["document"])
        related_docs.update(module.get("related_documents", []))

    return {
        "status": status,
        "changed_source_files": sorted({change.path for change in source_changes}),
        "affected_modules": sorted(module["name"] for module in affected.values()),
        "related_documents": sorted(related_docs) if affected else [],
        "unmapped_files": sorted(unmapped),
    }


def print_impact(result: dict) -> None:
    print("Documentation Impact Analysis")
    print(f"Status: {result['status']}")
    if result["changed_source_files"]:
        print("\nChanged source files:")
        for path in result["changed_source_files"]:
            print(f"  - {path}")
    if result["affected_modules"]:
        print("\nPotentially affected modules:")
        for name in result["affected_modules"]:
            print(f"  - {name}")
        print("\nRelated documentation:")
        for path in result["related_documents"]:
            print(f"  - {path}")
    if result["unmapped_files"]:
        print("\nUNMAPPED SOURCE FILE:")
        for path in result["unmapped_files"]:
            print(f"  - {path}")
    if result["status"] in {"REVIEW_REQUIRED", "DOCS_UPDATED"}:
        print(
            "\nNote: path coverage cannot prove semantic consistency; inspect the diff "
            "and update only facts that changed."
        )


def _without_fenced_code(text: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = "```" if stripped.startswith("```") else "~~~" if stripped.startswith("~~~") else None
        if marker:
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None:
            lines.append(line)
    return "\n".join(lines)


def validate_links(repo_root: Path, documents: list[str]) -> list[str]:
    errors: list[str] = []
    for relative in documents:
        document = repo_root / relative
        if not document.exists():
            errors.append(f"INVALID_REFERENCE missing document: {relative}")
            continue
        text = _without_fenced_code(document.read_text(encoding="utf-8"))
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not target:
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"INVALID_REFERENCE {relative} -> {raw_target}")
    return errors


def validate_source_references(repo_root: Path, documents: list[str]) -> list[str]:
    errors: list[str] = []
    for relative in documents:
        document = repo_root / relative
        if not document.exists():
            continue
        for source in SOURCE_RE.findall(document.read_text(encoding="utf-8")):
            matches = list(repo_root.glob(source)) if any(char in source for char in "*?[") else [repo_root / source]
            if not matches or not any(path.exists() for path in matches):
                errors.append(f"INVALID_REFERENCE {relative} -> {source}")
    return errors


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.ClassDef):
            symbols.add(node.name)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.add(f"{node.name}.{member.name}")
    return symbols


def validate_mapping(repo_root: Path, mapping: dict) -> list[str]:
    errors: list[str] = []
    for module in mapping["modules"]:
        document = repo_root / module["document"]
        if not document.exists():
            errors.append(f"INVALID_REFERENCE missing module document: {module['document']}")
        for symbol in module.get("symbols", []):
            source = repo_root / symbol["path"]
            if not source.exists():
                errors.append(f"INVALID_REFERENCE missing source: {symbol['path']}")
                continue
            try:
                definitions = _defined_symbols(source)
            except (OSError, SyntaxError) as exc:
                errors.append(f"INVALID_REFERENCE cannot parse {symbol['path']}: {exc}")
                continue
            if symbol["name"] not in definitions:
                errors.append(
                    f"INVALID_REFERENCE missing symbol: {symbol['path']}::{symbol['name']}"
                )
    return errors


def _expand_snippets(repo_root: Path, text: str) -> tuple[str, list[str]]:
    errors: list[str] = []

    def replace(match: re.Match[str]) -> str:
        snippet_path = repo_root / match.group(1)
        if not snippet_path.exists():
            errors.append(f"INVALID_REFERENCE missing snippet: {match.group(1)}")
            return ""
        return snippet_path.read_text(encoding="utf-8")

    return SNIPPET_RE.sub(replace, text), errors


def validate_mermaid(repo_root: Path, mapping: dict) -> list[str]:
    errors: list[str] = []
    for relative, expected in mapping["mermaid_documents"].items():
        path = repo_root / relative
        if not path.exists():
            errors.append(f"INVALID_REFERENCE missing Mermaid document: {relative}")
            continue
        expanded, snippet_errors = _expand_snippets(
            repo_root, path.read_text(encoding="utf-8")
        )
        errors.extend(snippet_errors)
        count = len(re.findall(r"^```mermaid\s*$", expanded, flags=re.MULTILINE))
        if count != expected:
            errors.append(f"INVALID_REFERENCE Mermaid count {relative}: {count}, expected {expected}")
    return errors


def validate_versions(repo_root: Path, mapping: dict) -> list[str]:
    errors: list[str] = []
    expected_sha = mapping["verified_against"]
    expected_date = mapping["last_verified"]
    for relative in mapping["knowledge_documents"]:
        if not relative.startswith("docs/design/") or relative.endswith("MAINTENANCE.md"):
            continue
        path = repo_root / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if expected_sha not in text or expected_date not in text:
            errors.append(f"INVALID_REFERENCE missing verification metadata: {relative}")
    return errors


def run_build(repo_root: Path) -> tuple[list[str], str]:
    configured = os.environ.get("FORGE_AGENT_DOCS_PYTHON")
    default = Path.home() / ".venvs/forge-agent-docs/bin/python"
    if configured:
        command = [configured, "-m", "zensical", "build", "--clean", "--strict"]
    elif default.exists():
        command = [str(default), "-m", "zensical", "build", "--clean", "--strict"]
    elif shutil.which("zensical"):
        command = ["zensical", "build", "--clean", "--strict"]
    else:
        return ["INVALID_REFERENCE Zensical is not installed in the docs environment"], ""

    result = _run(command, repo_root, check=False)
    output = result.stdout + result.stderr
    if result.returncode:
        return [f"INVALID_REFERENCE Zensical build failed ({result.returncode})"], output
    return [], output


def validate_built_site(repo_root: Path, mapping: dict) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    site = repo_root / "site"
    required = [
        "index.html",
        "source-navigation.html",
        "interview-review.html",
        "reference.html",
        "search.json",
        "javascripts/iframe-worker-shim.js",
        "stylesheets/extra.css",
    ]
    for relative in required:
        if not (site / relative).exists():
            errors.append(f"INVALID_REFERENCE missing built asset: site/{relative}")

    for excluded in ("changes", "evidence", "plans", "todo"):
        if (site / excluded).exists():
            errors.append(f"INVALID_REFERENCE excluded site directory was built: site/{excluded}")

    expected_pages = {
        "docs/README.md": "index.html",
        "docs/design/Forge-Agent-当前代码架构.md": "design/Forge-Agent-当前代码架构.html",
        "docs/design/01-任务执行主链.md": "design/01-任务执行主链.html",
        "docs/design/02-Agent与Tool生命周期.md": "design/02-Agent与Tool生命周期.html",
        "docs/design/03-Context与RepoMap.md": "design/03-Context与RepoMap.html",
        "docs/design/04-Planning与Recovery.md": "design/04-Planning与Recovery.html",
        "docs/design/05-Skills与MCP.md": "design/05-Skills与MCP.html",
        "docs/design/06-隔离与独立验收.md": "design/06-隔离与独立验收.html",
        "docs/design/07-Evaluation与Evolution.md": "design/07-Evaluation与Evolution.html",
        "docs/design/MAINTENANCE.md": "design/MAINTENANCE.html",
    }
    for document, expected_count in mapping["mermaid_documents"].items():
        page = site / expected_pages[document]
        if not page.exists():
            errors.append(f"INVALID_REFERENCE missing built page: {page.relative_to(repo_root)}")
            continue
        count = page.read_text(encoding="utf-8").count('class="mermaid"')
        if count != expected_count:
            errors.append(
                f"INVALID_REFERENCE rendered Mermaid count {page.relative_to(repo_root)}: "
                f"{count}, expected {expected_count}"
            )

    for html_path in site.rglob("*.html"):
        text = html_path.read_text(encoding="utf-8")
        if re.search(r"<(?:script|link)[^>]+(?:src|href)=[\"']https?://", text):
            errors.append(f"INVALID_REFERENCE remote runtime asset: {html_path.relative_to(repo_root)}")
    for css_path in site.rglob("*.css"):
        if re.search(r"url\(https?://", css_path.read_text(encoding="utf-8")):
            errors.append(f"INVALID_REFERENCE remote CSS asset: {css_path.relative_to(repo_root)}")

    matches: dict[str, str] = {}
    search_path = site / "search.json"
    if search_path.exists():
        payload = json.loads(search_path.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        for term in mapping["search_terms"]:
            folded = term.casefold()
            found = next(
                (
                    item
                    for item in items
                    if folded
                    in html.unescape(
                        TAG_RE.sub(" ", f"{item.get('title', '')} {item.get('text', '')}")
                    ).casefold()
                ),
                None,
            )
            if found is None:
                errors.append(f"INVALID_REFERENCE search term missing: {term}")
            else:
                matches[term] = found.get("location", "")
    return errors, matches


def validate_all(repo_root: Path, mapping: dict, *, build: bool) -> list[str]:
    documents = mapping["knowledge_documents"]
    errors = []
    errors.extend(validate_links(repo_root, documents))
    errors.extend(validate_source_references(repo_root, documents))
    errors.extend(validate_mapping(repo_root, mapping))
    errors.extend(validate_mermaid(repo_root, mapping))
    errors.extend(validate_versions(repo_root, mapping))
    if build and not errors:
        build_errors, output = run_build(repo_root)
        print(output.strip())
        errors.extend(build_errors)
        if not build_errors:
            site_errors, matches = validate_built_site(repo_root, mapping)
            errors.extend(site_errors)
            for term, location in matches.items():
                print(f"SEARCH {term} -> {location}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze Git changes for architecture-document impact and validate the "
            "Markdown/Mermaid/Zensical knowledge base."
        )
    )
    parser.add_argument(
        "--base",
        help=(
            "Compare BASE..HEAD (two-dot) and also include staged, unstaged and untracked "
            "changes. Renames are handled as delete/add pairs. Defaults to HEAD when "
            "running impact analysis."
        ),
    )
    parser.add_argument(
        "--changed",
        action="append",
        default=[],
        metavar="PATH",
        help="Analyze an explicit changed path instead of reading Git; repeat as needed.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Check internal links, source paths, symbols, Mermaid counts and version metadata.",
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run strict Zensical build and verify pages, search results and offline assets.",
    )
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP, help="Architecture map path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    mapping = load_mapping(repo_root, args.map)
    failed = False

    run_impact = bool(args.changed or args.base is not None or not (args.validate or args.build))
    if run_impact:
        try:
            changes = (
                [ChangedPath("M", path.replace("\\", "/")) for path in args.changed]
                if args.changed
                else collect_git_changes(repo_root, args.base or "HEAD")
            )
        except subprocess.CalledProcessError as exc:
            print(exc.stderr.strip() or str(exc), file=sys.stderr)
            return 2
        impact = analyze_changes(changes, mapping)
        print_impact(impact)
        failed = impact["status"] in {"REVIEW_REQUIRED", "UNMAPPED"}

    if args.validate or args.build:
        errors = validate_all(repo_root, mapping, build=args.build)
        if errors:
            print("\nDocumentation Consistency: INVALID_REFERENCE")
            for error in errors:
                print(f"  - {error}")
            failed = True
        else:
            print("\nDocumentation Consistency: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
