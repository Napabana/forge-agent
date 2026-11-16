"""Interactive chat input with command and repository path completion."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion


COMMANDS = ("/exit", "/stats", "/session", "/new", "/resume", "/rename",
            "/clear", "/help", "/model")


class ChatCompleter(Completer):
    def __init__(self, repo_path: Path, model_names=()):
        self.repo_path = repo_path
        self.model_names = model_names

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        if before.startswith("/") and not re.search(r"\s", before):
            for command in COMMANDS:
                if command.startswith(before):
                    yield Completion(command, start_position=-len(before))
            return
        if before.startswith("/model "):
            prefix = before.removeprefix("/model ")
            for name in self.model_names:
                if name.startswith(prefix):
                    yield Completion(name, start_position=-len(prefix))
            return
        match = re.search(r"(?:^|\s)@([^\s]*)$", before)
        if match is None:
            return
        typed = match.group(1)
        parent, _, prefix = typed.rpartition("/")
        folder = (self.repo_path / parent).resolve()
        if not folder.is_relative_to(self.repo_path.resolve()) or not folder.is_dir():
            return
        try:
            for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if item.name.startswith(".") or not item.name.lower().startswith(prefix.lower()):
                    continue
                suffix = "/" if item.is_dir() else ""
                yield Completion(item.name + suffix, start_position=-len(prefix))
        except OSError:
            return


def chat_prompt(repo_path: Path, model_names=()):
    """Use a live menu on a terminal; keep piped input compatible with Click tests."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    return PromptSession(completer=ChatCompleter(repo_path, model_names), complete_while_typing=True)
