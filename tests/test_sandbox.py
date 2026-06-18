"""
tests/test_sandbox.py

沙箱 Runtime 测试。
DockerRuntime 的真实 docker exec 路径需要 Docker 可用，
用 pytest.mark.skipif 跳过（CI / 没有 Docker 的环境）。
LocalRuntime 的测试不依赖 Docker，始终运行。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.runtime import (
    DockerRuntime, LocalRuntime, RunResult, create_runtime,
    build_docker_run_args,
    CONTAINER_WORKDIR, SANDBOX_IMAGE,
)
from tools.shell_tool import ShellTool
from tools.test_tool import PytestTool
from tools.git_tool import GitStatusTool


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

DOCKER_AVAILABLE = shutil.which("docker") is not None and (
    subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
)


# ===========================================================================
# RunResult
# ===========================================================================

class TestRunResult:
    def test_success(self):
        r = RunResult(returncode=0, stdout="hello\n", stderr="")
        assert r.success
        assert r.output == "hello\n"

    def test_failure(self):
        r = RunResult(returncode=1, stdout="", stderr="error")
        assert not r.success

    def test_output_combines_stdout_stderr(self):
        r = RunResult(returncode=0, stdout="out", stderr="err")
        assert r.output == "outerr"


# ===========================================================================
# LocalRuntime
# ===========================================================================

class TestLocalRuntime:
    def test_exec_simple_command(self):
        rt = LocalRuntime()
        result = rt.exec("echo hello")
        assert result.success
        assert "hello" in result.output

    def test_exec_with_cwd(self, tmp_path):
        (tmp_path / "test.txt").write_text("content")
        rt = LocalRuntime()
        result = rt.exec("ls", cwd=str(tmp_path))
        assert result.success
        assert "test.txt" in result.output

    def test_exec_failure(self):
        rt = LocalRuntime()
        result = rt.exec("false")
        assert not result.success
        assert result.returncode != 0

    def test_exec_timeout(self):
        rt = LocalRuntime()
        result = rt.exec("sleep 10", timeout=1)
        assert not result.success
        assert "timed out" in result.stderr.lower()

    def test_name(self):
        assert LocalRuntime().name == "local"

    def test_context_manager(self):
        with LocalRuntime() as rt:
            result = rt.exec("echo ctx")
        assert "ctx" in result.output

    def test_cleanup_is_noop(self):
        rt = LocalRuntime()
        rt.cleanup()  # 不应抛异常


# ===========================================================================
# ShellTool with LocalRuntime (默认)
# ===========================================================================

class TestShellToolWithRuntime:
    def test_default_uses_local_runtime(self):
        tool = ShellTool()
        assert isinstance(tool._runtime, LocalRuntime)

    def test_custom_runtime_injected(self):
        mock_rt = MagicMock()
        mock_rt.exec.return_value = RunResult(returncode=0, stdout="mocked\n", stderr="")
        tool = ShellTool(runtime=mock_rt)
        result = tool.execute({"cmd": "echo test"})
        # echo 是只读命令，直接走 runtime
        assert result.success
        mock_rt.exec.assert_called_once()

    def test_runtime_receives_correct_cmd(self):
        calls = []
        class RecordingRuntime(LocalRuntime):
            def exec(self, cmd, cwd=None, timeout=30):
                calls.append(cmd)
                return super().exec(cmd, cwd=cwd, timeout=timeout)

        tool = ShellTool(runtime=RecordingRuntime())
        tool.execute({"cmd": "echo hello"})
        assert len(calls) == 1
        assert calls[0] == "echo hello"

    def test_blocked_command_never_reaches_runtime(self):
        mock_rt = MagicMock()
        tool = ShellTool(runtime=mock_rt)
        result = tool.execute({"cmd": "rm -rf /"})
        assert not result.success
        assert "blocked" in result.error.lower()
        mock_rt.exec.assert_not_called()

    def test_denied_command_never_reaches_runtime(self):
        from tools.shell_tool import always_deny
        mock_rt = MagicMock()
        tool = ShellTool(confirm_callback=always_deny, runtime=mock_rt)
        result = tool.execute({"cmd": "pip install requests"})
        assert not result.success
        mock_rt.exec.assert_not_called()


# ===========================================================================
# PytestTool with runtime
# ===========================================================================

class TestTestToolWithRuntime:
    def test_default_uses_local_runtime(self):
        tool = PytestTool()
        assert isinstance(tool._runtime, LocalRuntime)

    def test_custom_runtime_used(self, tmp_path):
        # 用真实的 LocalRuntime，但通过 tmp_path 验证 cwd 传递
        calls = []
        class RecordingRuntime(LocalRuntime):
            def exec(self, cmd, cwd=None, timeout=30):
                calls.append({"cmd": cmd, "cwd": cwd})
                return super().exec(cmd, cwd=cwd, timeout=timeout)

        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text("def test_ok(): assert True\n")

        tool = PytestTool(runtime=RecordingRuntime())
        tool.execute({"path": str(tests_dir), "cwd": str(tmp_path)})
        assert len(calls) == 1
        assert "pytest" in calls[0]["cmd"]


# ===========================================================================
# GitTool with runtime
# ===========================================================================

class TestGitToolWithRuntime:
    def test_default_uses_local_runtime(self):
        tool = GitStatusTool()
        assert isinstance(tool._runtime, LocalRuntime)

    def test_custom_runtime_called(self):
        calls = []
        class RecordingRuntime(LocalRuntime):
            def exec(self, cmd, cwd=None, timeout=30):
                calls.append(cmd)
                return super().exec(cmd, cwd=cwd, timeout=timeout)

        tool = GitStatusTool(runtime=RecordingRuntime())
        tool.execute({})
        assert any("git" in c for c in calls)


# ===========================================================================
# create_runtime 工厂函数
# ===========================================================================

class TestCreateRuntime:
    def test_no_sandbox_returns_local(self):
        rt = create_runtime(sandbox=False)
        assert isinstance(rt, LocalRuntime)

    def test_sandbox_without_repo_raises(self):
        with pytest.raises(ValueError, match="repo_path"):
            create_runtime(sandbox=True, repo_path=None)

    def test_sandbox_returns_docker_runtime(self, tmp_path):
        rt = create_runtime(sandbox=True, repo_path=str(tmp_path))
        assert isinstance(rt, DockerRuntime)
        # 不 start，不清理

    def test_local_runtime_context_manager(self):
        with create_runtime(sandbox=False) as rt:
            result = rt.exec("echo hi")
        assert "hi" in result.output


# ===========================================================================
# DockerRuntime — 单元测试（mock docker 调用）
# ===========================================================================

class TestDockerRuntimeUnit:
    """不需要真实 Docker 的单元测试。"""

    def _make_runtime(self, tmp_path) -> DockerRuntime:
        return DockerRuntime(repo_path=str(tmp_path))

    def test_name_includes_image(self, tmp_path):
        rt = self._make_runtime(tmp_path)
        assert "docker" in rt.name
        assert SANDBOX_IMAGE in rt.name

    def test_not_running_initially(self, tmp_path):
        rt = self._make_runtime(tmp_path)
        assert not rt.is_running
        assert rt.container_id is None

    def test_cleanup_when_not_running_is_safe(self, tmp_path):
        rt = self._make_runtime(tmp_path)
        rt.cleanup()   # 不应抛异常

    def test_docker_unavailable_returns_error(self, tmp_path):
        """Docker 不可用时 exec() 返回 error，不崩溃。"""
        rt = self._make_runtime(tmp_path)

        with patch("subprocess.run") as mock_run:
            # docker info 返回失败
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Cannot connect")
            result = rt.exec("echo hello")

        assert not result.success
        assert "docker" in result.stderr.lower() or "not available" in result.stderr.lower()

    def test_container_start_failure_returns_error(self, tmp_path):
        """容器启动失败时返回 error。"""
        rt = self._make_runtime(tmp_path)

        def mock_run(args, **kwargs):
            m = MagicMock()
            if "info" in args:
                m.returncode = 0   # docker info 成功
            else:
                m.returncode = 1   # docker run 失败
                m.stdout = ""
                m.stderr = "image not found"
            return m

        with patch("subprocess.run", side_effect=mock_run):
            result = rt.exec("echo hello")

        assert not result.success
        assert not rt.is_running

    def test_cwd_translation_inside_repo(self, tmp_path):
        """容器内 cwd：repo 子目录应被正确翻译为容器内路径。"""
        rt = DockerRuntime(repo_path=str(tmp_path))
        rt._container_id = "fake-container-id"

        sub = tmp_path / "src" / "module"
        sub.mkdir(parents=True)

        exec_calls = []

        def mock_run(args, **kwargs):
            exec_calls.append(args)
            m = MagicMock()
            m.returncode = 0
            m.stdout = "ok"
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run):
            rt.exec("ls", cwd=str(sub))

        # 找到 docker exec 调用
        docker_exec_call = next(a for a in exec_calls if "exec" in a)
        workdir_idx = docker_exec_call.index("--workdir")
        container_cwd = docker_exec_call[workdir_idx + 1]
        assert container_cwd == f"{CONTAINER_WORKDIR}/src/module"

    def test_cleanup_removes_container(self, tmp_path):
        """cleanup() 应调用 docker rm -f。"""
        rt = self._make_runtime(tmp_path)
        rt._container_id = "abc123"

        rm_calls = []
        def mock_run(args, **kwargs):
            rm_calls.append(args)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=mock_run):
            rt.cleanup()

        assert rt._container_id is None
        rm_call = next((a for a in rm_calls if "rm" in a), None)
        assert rm_call is not None
        assert "abc123" in rm_call

    # --- M3 加固参数透传到 docker run argv ---

    def _capture_run_args(self, rt, tmp_path):
        """触发 exec → 捕获 docker run 的 argv（mock subprocess.run）。"""
        runs = []

        def mock_run(args, **kwargs):
            runs.append(args)
            m = MagicMock()
            if "info" in args:
                m.returncode = 0; m.stdout = ""; m.stderr = ""
            elif "run" in args:
                m.returncode = 0; m.stdout = "fakecid\n"; m.stderr = ""
            elif "exec" in args:
                m.returncode = 0; m.stdout = "ok"; m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=mock_run):
            rt.exec("echo hi")
        return next(a for a in runs if "run" in a)

    def test_init_defaults_backward_compat(self, tmp_path):
        """回归守卫：裸 DockerRuntime(run) 的 run args 含加固默认值且保持旧行为。"""
        rt = DockerRuntime(repo_path=str(tmp_path))
        args = self._capture_run_args(rt, tmp_path)
        # 加固默认值（M3 新增）
        assert args[args.index("--memory") + 1] == "1g"
        assert args[args.index("--cpus") + 1] == "2.0"
        assert "--network" in args and args[args.index("--network") + 1] == "none"
        assert "--tmpfs" in args and args[args.index("--tmpfs") + 1] == "/tmp"
        # 旧行为保持：repo rw 挂载，无只读根
        assert args[args.index("-v") + 1] == f"{tmp_path}:{CONTAINER_WORKDIR}:rw"
        assert "--read-only" not in args
        assert "--nano-cpus" not in args   # 不该用这个无效 flag

    def test_init_readonly_root_adds_flag_and_ro_mount(self, tmp_path):
        rt = DockerRuntime(repo_path=str(tmp_path), readonly_root=True)
        args = self._capture_run_args(rt, tmp_path)
        assert "--read-only" in args
        assert args[args.index("-v") + 1] == f"{tmp_path}:{CONTAINER_WORKDIR}:ro"

    def test_init_mem_limit_propagates(self, tmp_path):
        rt = DockerRuntime(repo_path=str(tmp_path), mem_limit="512m")
        args = self._capture_run_args(rt, tmp_path)
        assert args[args.index("--memory") + 1] == "512m"

    def test_init_network_true_omits_network_none(self, tmp_path):
        rt = DockerRuntime(repo_path=str(tmp_path), network=True)
        args = self._capture_run_args(rt, tmp_path)
        assert "--network" not in args


# ===========================================================================
# build_docker_run_args — 纯函数单测（零 Docker 依赖）
# ===========================================================================

class TestBuildDockerRunArgs:
    """build_docker_run_args 是纯函数，任何环境都能跑。"""

    def _build(self, **kw):
        defaults = dict(container_name="c", repo_path="/repo", image="img")
        defaults.update(kw)
        return build_docker_run_args(**defaults)

    def test_starts_with_docker_run(self):
        a = self._build()
        assert a[:2] == ["docker", "run"]
        # 尾部：image + tail -f /dev/null
        assert a[-4:] == ["img", "tail", "-f", "/dev/null"]

    def test_basic_flags_present(self):
        a = self._build(container_name="mybox")
        assert "--detach" in a
        assert "--rm" in a
        assert "--workdir" in a
        assert a[a.index("--name") + 1] == "mybox"

    def test_default_repo_mount_is_rw(self):
        a = self._build()
        assert a[a.index("-v") + 1] == "/repo:/workspace:rw"

    def test_readonly_root_makes_repo_ro(self):
        a = self._build(readonly_root=True)
        assert a[a.index("-v") + 1] == "/repo:/workspace:ro"
        assert "--read-only" in a

    def test_readonly_root_false_no_read_only_flag(self):
        a = self._build(readonly_root=False)
        assert "--read-only" not in a

    def test_memory_default_and_custom(self):
        assert self._build()[self._build().index("--memory") + 1] == "1g"
        a = self._build(mem_limit="512m")
        assert a[a.index("--memory") + 1] == "512m"

    def test_cpus_default_and_custom(self):
        # 默认 2 核 → --cpus 2.0
        a = self._build()
        assert a[a.index("--cpus") + 1] == "2.0"
        # nano_cpus=1e9 → 1 核
        a2 = self._build(nano_cpus=1_000_000_000)
        assert a2[a2.index("--cpus") + 1] == "1.0"
        assert "--nano-cpus" not in a2

    @pytest.mark.parametrize("readonly", [True, False])
    def test_tmpfs_always_present(self, readonly):
        a = self._build(readonly_root=readonly)
        assert a[a.index("--tmpfs") + 1] == "/tmp"

    def test_network_none_by_default(self):
        a = self._build()
        assert "--network" in a and a[a.index("--network") + 1] == "none"

    def test_network_true_omits_none(self):
        a = self._build(network=True)
        assert "--network" not in a

    def test_worktree_mount_is_rw_after_repo(self):
        a = self._build(worktree_mount=("/host/wt", "/workspace"))
        vs = [a[i + 1] for i, t in enumerate(a) if t == "-v"]
        assert "/repo:/workspace:rw" in vs
        assert "/host/wt:/workspace:rw" in vs
        # worktree 挂载在 repo 之后（rw 盖住 ro）
        assert vs.index("/host/wt:/workspace:rw") > vs.index("/repo:/workspace:rw")

    def test_worktree_rw_even_when_readonly_root(self):
        a = self._build(readonly_root=True, worktree_mount=("/host/wt", "/workspace"))
        vs = [a[i + 1] for i, t in enumerate(a) if t == "-v"]
        assert "/repo:/workspace:ro" in vs
        assert "/host/wt:/workspace:rw" in vs   # worktree 始终可写

    def test_extra_mounts_appended_in_order(self):
        a = self._build(extra_mounts=[("/a", "/ca"), ("/b", "/cb")])
        vs = [a[i + 1] for i, t in enumerate(a) if t == "-v"]
        assert "/a:/ca" in vs and "/b:/cb" in vs
        assert vs.index("/a:/ca") < vs.index("/b:/cb")

    def test_no_worktree_no_extra_exactly_one_mount(self):
        a = self._build()
        assert a.count("-v") == 1   # 只有 repo 挂载


# ===========================================================================
# create_runtime — 工厂函数（参数透传 + 死代码已删）
# ===========================================================================

class TestCreateRuntimeHardened:
    def test_network_true_propagates(self, tmp_path):
        with patch("tools.runtime.DockerRuntime") as mk:
            create_runtime(sandbox=True, repo_path=str(tmp_path), network=True)
        assert mk.call_args.kwargs["network"] is True

    def test_readonly_and_worktree_propagate(self, tmp_path):
        with patch("tools.runtime.DockerRuntime") as mk:
            create_runtime(
                sandbox=True, repo_path=str(tmp_path),
                readonly_root=True, worktree_mount=("/h", "/workspace"),
            )
        assert mk.call_args.kwargs["readonly_root"] is True
        assert mk.call_args.kwargs["worktree_mount"] == ("/h", "/workspace")

    def test_no_allow_network_attribute(self, tmp_path):
        """死代码 _allow_network 已删除。"""
        with patch("subprocess.run"):   # 阻止真起容器
            rt = create_runtime(sandbox=True, repo_path=str(tmp_path))
        assert not hasattr(rt, "_allow_network")


# ===========================================================================
# DockerRuntime — 集成测试（需要真实 Docker）
# ===========================================================================

@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available")
class TestDockerRuntimeIntegration:

    def test_exec_simple_command(self, tmp_path):
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            result = rt.exec("echo hello_from_docker")
        assert result.success
        assert "hello_from_docker" in result.output

    def test_exec_python(self, tmp_path):
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            result = rt.exec("python3 -c \"print('python ok')\"")
        assert result.success
        assert "python ok" in result.output

    def test_file_visible_in_container(self, tmp_path):
        """宿主机写入的文件在容器里可见。"""
        (tmp_path / "hello.txt").write_text("from host")
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            result = rt.exec("cat hello.txt")
        assert result.success
        assert "from host" in result.output

    def test_file_written_in_container_visible_on_host(self, tmp_path):
        """容器写入的文件在宿主机可见（bind mount 双向）。"""
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            rt.exec("echo from_container > container_output.txt")
        content = (tmp_path / "container_output.txt").read_text()
        assert "from_container" in content

    def test_no_network_by_default(self, tmp_path):
        """默认断网，curl 应失败。"""
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            result = rt.exec("curl -s --max-time 3 https://example.com", timeout=10)
        assert not result.success

    def test_cleanup_stops_container(self, tmp_path):
        rt = DockerRuntime(repo_path=str(tmp_path))
        rt.exec("echo start")   # 触发容器启动
        container_id = rt.container_id
        assert container_id is not None
        rt.cleanup()
        assert rt.container_id is None
        # 确认容器已被删除
        check = subprocess.run(
            ["docker", "inspect", container_id],
            capture_output=True, timeout=5,
        )
        assert check.returncode != 0  # 容器不存在

    def test_shell_tool_with_docker_runtime(self, tmp_path):
        """ShellTool + DockerRuntime 端到端。"""
        with DockerRuntime(repo_path=str(tmp_path)) as rt:
            tool = ShellTool(runtime=rt)
            result = tool.execute({"cmd": "python3 --version"})
        assert result.success
        assert "Python" in result.output

    # --- M3 加固参数的真实容器行为验证 ---

    def test_read_only_root_blocks_write_to_workspace(self, tmp_path):
        """readonly_root=True：写 /workspace 应失败（read-only file system）。"""
        with DockerRuntime(repo_path=str(tmp_path), readonly_root=True) as rt:
            result = rt.exec("touch /workspace/x", timeout=15)
        assert not result.success
        assert "read-only" in result.output.lower()

    def test_tmpfs_writable_when_readonly_root(self, tmp_path):
        """readonly_root=True 下 /tmp（tmpfs）仍可写。"""
        with DockerRuntime(repo_path=str(tmp_path), readonly_root=True) as rt:
            result = rt.exec("echo ok > /tmp/y && cat /tmp/y", timeout=15)
        assert result.success
        assert "ok" in result.output

    def test_memory_limit_reflected_in_inspect(self, tmp_path):
        """--memory 1g 应在 docker inspect 里体现。"""
        rt = DockerRuntime(repo_path=str(tmp_path))
        try:
            rt.exec("echo start")   # 触发启动
            cid = rt.container_id
            assert cid is not None
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.HostConfig.Memory}}", cid],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            assert out == "1073741824"   # 1g = 1024^3
        finally:
            rt.cleanup()

    def test_cpu_limit_reflected_in_inspect(self, tmp_path):
        """--cpus 2.0 应在 docker inspect 里体现（NanoCpus=2e9）。"""
        rt = DockerRuntime(repo_path=str(tmp_path))
        try:
            rt.exec("echo start")
            cid = rt.container_id
            assert cid is not None
            out = subprocess.run(
                ["docker", "inspect", "-f", "{{.HostConfig.NanoCpus}}", cid],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            assert out == "2000000000"   # 2 核
        finally:
            rt.cleanup()

    def test_network_true_allows_egress(self, tmp_path):
        """network=True：可访问外网（与默认断网对照）。"""
        with DockerRuntime(repo_path=str(tmp_path), network=True) as rt:
            result = rt.exec(
                "python3 -c \"import urllib.request; "
                "urllib.request.urlopen('https://example.com', timeout=5)\" 2>&1 | head -1",
                timeout=30,
            )
        # 默认 bridge 网络下应能连通（成功或重定向都算，关键是没被 DNS/网络拒绝）
        assert not ("Temporary failure" in result.output
                    or "Network is unreachable" in result.output)


# ===========================================================================
# CLI --sandbox 选项
# ===========================================================================

class TestCliSandboxOption:
    def test_sandbox_in_run_help(self):
        from click.testing import CliRunner
        from entry.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "--help"])
        assert "--sandbox" in result.output

    def test_sandbox_in_chat_help(self):
        from click.testing import CliRunner
        from entry.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["chat", "--help"])
        assert "--sandbox" in result.output