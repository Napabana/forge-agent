"""Compatibility facade for observers using the original renderer options."""
from config.schema import UIConfig
from entry.ui.terminal import TerminalUI


class RunEventRenderer(TerminalUI):
    def __init__(self, preview_lines=5, compact=False, show_task=True, show_reasoning=False):
        super().__init__(UIConfig(tool_output_preview_lines=preview_lines,
                                  tool_output_mode="on_error" if compact else "summary"),
                         no_progress=True, show_task=show_task, show_reasoning=show_reasoning)

    def record_streamed_text(self, text):
        self.last_answer += text
