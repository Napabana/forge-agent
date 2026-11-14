import json
import os
import sys

import pytest

from tools.execution_env import execution_environment
from tools.runtime import LocalRuntime
from evals.coding_agent.graders import _run_command


@pytest.mark.parametrize("mode", ["argv", "shell", "grader"])
def test_untrusted_child_does_not_inherit_provider_credentials(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "fake-security-test-only")
    monkeypatch.setenv("CUSTOM_VENDOR_CREDENTIAL", "fake-custom-test-only")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/startup")
    script = "import os,json; print(json.dumps({k:k in os.environ for k in ['SILICONFLOW_API_KEY','CUSTOM_VENDOR_CREDENTIAL','PYTHONPATH','PATH']}))"
    if mode == "grader":
        proc = _run_command(tmp_path, [sys.executable, "-c", script], 10)
    elif mode == "argv":
        proc = LocalRuntime().exec_args([sys.executable, "-c", script], cwd=str(tmp_path))
    else:
        import shlex
        proc = LocalRuntime().exec(shlex.join([sys.executable, "-c", script]), cwd=str(tmp_path))
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == dict(SILICONFLOW_API_KEY=False, CUSTOM_VENDOR_CREDENTIAL=False, PYTHONPATH=False, PATH=True)
    assert os.environ["SILICONFLOW_API_KEY"] == "fake-security-test-only"


def test_environment_is_allowlist_not_secret_name_blacklist():
    assert "INVENTED_SECRET" not in execution_environment({"INVENTED_SECRET": "fake", "PATH": "/bin"})
    assert execution_environment({"PATH": "/bin"})["PATH"] == "/bin"
