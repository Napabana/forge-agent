"""pr-test Issue #4 的仓库外独立验收器。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


repo = Path.cwd()
# 先验证目标仓库原有回归，再加载本轮实现检查隐藏边界。
subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=repo, check=True)
spec = importlib.util.spec_from_file_location("calculator", repo / "calculator.py")
if spec is None or spec.loader is None:
    raise RuntimeError("calculator.py cannot be loaded")
calculator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calculator)

assert calculator.add(-2, -3) == -5
assert calculator.divide(7, 2) == 3.5
assert calculator.clamp(-1, 0, 10) == 0
assert calculator.clamp(11, 0, 10) == 10
assert calculator.clamp(5, 0, 10) == 5
assert calculator.clamp(4, 4, 4) == 4
assert any("\u4e00" <= char <= "\u9fff" for char in (calculator.clamp.__doc__ or ""))
try:
    calculator.clamp(1, 2, 0)
except ValueError as exc:
    assert str(exc) == "lower must not exceed upper"
else:
    raise AssertionError("lower greater than upper must raise ValueError")
