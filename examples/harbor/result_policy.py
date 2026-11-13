from __future__ import annotations

from collections.abc import Mapping
from typing import Any


GRADABLE_NONZERO_STATUSES = frozenset({"incomplete", "gave_up"})


def is_benchmark_outcome(usage: Mapping[str, Any]) -> bool:
    """Return whether a non-zero Forge exit is a gradable model outcome."""
    return usage.get("run_status") in GRADABLE_NONZERO_STATUSES
