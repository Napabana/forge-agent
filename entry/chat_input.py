"""Interactive chat input and inline terminal selection."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from entry.ui.terminal import safe_text


COMMANDS = {
    "/help": "Show commands",
    "/model": "Select a model",
    "/status": "Show session and workspace",
    "/new": "Start a fresh session",
    "/resume": "Resume a round or session",
    "/exit": "Exit chat",
    "/stats": "Show session statistics",
    "/session": "Show session ID and storage",
    "/rewind": "Branch from a saved round",
    "/rename": "Rename this session",
    "/clear": "Clear the conversation",
    "/diff": "Show the latest patch",
    "/tokens": "Show model usage",
    "/plan": "Inspect the plan",
    "/recovery": "Inspect recovery decisions",
    "/archive": "Inspect archived context",
    "/cache": "Inspect request cache records",
    "/skills": "Show loaded skills",
    "/mcp": "Show connected tools",
    "/compact": "Inspect context compaction",
}

_STYLE = Style.from_dict({
    "bottom-toolbar": "noreverse",
    "bottom-toolbar.text": "noreverse",
    "forge-selected": "bold ansicyan",
    "forge-detail": "dim",
})


def _slash_prefix(text: str) -> bool:
    return text.startswith("/") and not any(char.isspace() for char in text)


def _trim_to_columns(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(value) <= width:
        return value
    remaining = width - 1
    for index, char in enumerate(value):
        remaining -= get_cwidth(char)
        if remaining < 0:
            return value[:index] + "…"
    return value


@dataclass(frozen=True)
class _Choice:
    key: str
    label: str
    detail: str
    current: bool = False


class _ChoiceList:
    """Selection and display shared by command completion and model search."""

    def __init__(self, choices, *, prefix=False):
        self.choices = tuple(choices)
        self.prefix = prefix
        self.matches = self.choices
        self.query = None
        self.index = 0

    def update(self, query):
        query = query.casefold()
        if query == self.query:
            return
        self.query = query
        if self.prefix:
            self.matches = tuple(item for item in self.choices if item.key.startswith(query))
        else:
            words = query.split()
            self.matches = tuple(
                item for item in self.choices
                if all(word in f"{item.key} {item.label} {item.detail}".casefold() for word in words)
            )
        self.index = next((i for i, item in enumerate(self.matches) if item.current and not query), 0)

    def selected(self):
        return self.matches[self.index] if self.matches else None

    def bind_arrows(self, keys, active=True):
        @keys.add("up", filter=active)
        def previous(event):
            self.update(event.current_buffer.text)
            if self.matches:
                self.index = (self.index - 1) % len(self.matches)

        @keys.add("down", filter=active)
        def next_choice(event):
            self.update(event.current_buffer.text)
            if self.matches:
                self.index = (self.index + 1) % len(self.matches)

    def render(self):
        size = get_app().output.get_size()
        count = min(5, max(1, size.rows - 5))
        start = max(0, min(self.index - count // 2, len(self.matches) - count))
        visible = self.matches[start:start + count]
        if not visible:
            return [("class:forge-detail", "  No matches\n")]
        width = size.columns - 4
        show_details = width >= 32
        label_width = (min(max(get_cwidth(item.label) for item in visible), width * 2 // 3)
                       if show_details else width)
        rows = []
        for index, item in enumerate(visible, start):
            selected = index == self.index
            marker = "› " if selected else "  "
            current = "✓ " if item.current else "  "
            label = _trim_to_columns(item.label, label_width)
            rows.append(("class:forge-selected" if selected else "", marker + current + label))
            if show_details and item.detail:
                gap = " " * (label_width - get_cwidth(label) + 2)
                detail = _trim_to_columns(item.detail, width - label_width - 2)
                rows.append(("class:forge-detail", gap + detail))
            rows.append(("", "\n"))
        if len(self.matches) > count:
            rows.append(("class:forge-detail", f"  {self.index + 1}/{len(self.matches)}\n"))
        return rows


class ChatCompleter(Completer):
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def get_completions(self, document, complete_event):
        before = document.text_before_cursor
        if _slash_prefix(before):
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


def chat_prompt(repo_path: Path, footer=None):
    """Show command choices below the input and retain Tab completion for paths."""
    if (not sys.stdin.isatty() or not sys.stdout.isatty() or not sys.stderr.isatty()
            or os.getenv("TERM") == "dumb" or os.getenv("CI", "").lower() in ("1", "true", "yes")):
        return None
    keys = KeyBindings()
    menu = _ChoiceList((_Choice(command, command, hint) for command, hint in COMMANDS.items()), prefix=True)
    command_input = Condition(lambda: _slash_prefix(get_app().current_buffer.text))
    menu.bind_arrows(keys, command_input)

    @keys.add("escape", "enter")
    def newline(event):
        event.current_buffer.insert_text("\n")

    @keys.add("c-g")
    def edit(event):
        event.current_buffer.open_in_editor()

    @keys.add("tab", filter=command_input)
    def complete_command(event):
        buffer = event.current_buffer
        menu.update(buffer.text)
        if choice := menu.selected():
            buffer.text = choice.key
            buffer.cursor_position = len(choice.key)

    @keys.add("enter", filter=command_input)
    def submit_command(event):
        complete_command(event)
        event.current_buffer.validate_and_handle()

    def below_input():
        fragments = []
        text = get_app().current_buffer.text
        if _slash_prefix(text):
            menu.update(text)
            fragments.extend(menu.render())
        if footer:
            value = footer()
            if value:
                width = get_app().output.get_size().columns
                fragments.append(("class:forge-detail", _trim_to_columns(safe_text(value), width)))
        if fragments and fragments[-1][1] == "\n":
            fragments.pop()
        return fragments

    session = PromptSession(completer=ChatCompleter(repo_path), complete_while_typing=False, key_bindings=keys,
                            style=_STYLE, bottom_toolbar=below_input if footer else None)
    if footer is None:
        def update_toolbar(buffer):
            session.bottom_toolbar = below_input if _slash_prefix(buffer.text) else None
        session.default_buffer.on_text_changed += update_toolbar
    return session


def choose_model(profiles, current, *, allow_default):
    """Search configured profiles and return their key and persistence choice."""
    current_key = next((key for key, profile in profiles.items() if profile == current), None)
    menu = _ChoiceList(
        _Choice(key, safe_text(profile.model).replace("\n", " "),
                safe_text(f"{key} · {profile.provider}").replace("\n", " "), key == current_key)
        for key, profile in profiles.items()
    )
    keys = KeyBindings()
    menu.bind_arrows(keys)

    @keys.add("enter")
    def select(event):
        accept(event, save=False)

    if allow_default:
        @keys.add("c-s")
        def save_default(event):
            accept(event, save=True)

    def accept(event, save):
        menu.update(event.current_buffer.text)
        if choice := menu.selected():
            event.app.exit(result=(choice.key, save))

    @keys.add("escape")
    @keys.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    def below_search():
        menu.update(get_app().current_buffer.text)
        width = get_app().output.get_size().columns
        hint = "Enter switch · Ctrl+S save · Esc cancel" if allow_default else "Enter switch · Esc cancel"
        return [*menu.render(), ("class:forge-detail", _trim_to_columns(hint, width))]

    prompt = PromptSession(key_bindings=keys, style=_STYLE, bottom_toolbar=below_search)
    try:
        return prompt.prompt("model > ")
    except (KeyboardInterrupt, EOFError):
        return None
