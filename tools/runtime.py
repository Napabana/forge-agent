"""
tools/runtime.py

Runtime 抽象层：把"命令执行"从工具实现里解耦出来。

工具（ShellTool / PytestTool / GitTool）只负责构造命令参数，
Runtime 负责实际执行——本地 subprocess 或 Docker 容器。

设计原则：
- 工具层完全不感知 Runtime，通过依赖注入传入
- Runtime 可以在 ToolRegistry 创建时一次性注入，所有工具共享
- LocalRuntime 是默认行为（向后兼容，不传 runtime 等同于之前）
- DockerRuntime 管理容器生命周期，首次执行时懒启动容器

用法：
    # 默认本地
    registry = build_registry()

    # Docker 沙箱
    runtime = DockerRuntime(repo_path="/path/to/repo")
    registry = build_registry(runtime=runtime)
    # agent 跑完后清理
    runtime.cleanup()

    # 或者用上下文管理器自动清理
    with DockerRuntime(repo_path="/path/to/repo") as runtime:
        registry = build_registry(runtime=runtime)
        agent.run(task, log)
"""

from __future__ import annotations

import subprocess
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult — Runtime 执行结果
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Runtime 执行单条命令的结果。"""
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """合并 stdout + stderr，工具层直接用。"""
        return self.stdout + self.stderr


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class Runtime(ABC):
    """
    命令执行抽象基类。
    所有工具通过 runtime.exec() 执行命令，不直接调 subprocess。
    """

    @abstractmethod
    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        """
        执行 shell 命令，返回 RunResult。

        Args:
            cmd:     shell 命令字符串
            cwd:     工作目录（相对或绝对路径）
            timeout: 超时秒数

        Returns:
            RunResult，不抛异常（超时/错误封装在里面）
        """
        ...

    def cleanup(self) -> None:
        """释放 runtime 持有的资源（容器、连接等）。默认无操作。"""

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_) -> None:
        self.cleanup()

    @property
    @abstractmethod
    def name(self) -> str:
        """Runtime 名称，用于日志。"""
        ...


# ---------------------------------------------------------------------------
# LocalRuntime — 本地 subprocess（默认）
# ---------------------------------------------------------------------------

class LocalRuntime(Runtime):
    """
    本地执行，直接调 subprocess.run。
    行为和之前完全一致，是默认 runtime。
    """

    @property
    def name(self) -> str:
        return "local"

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s: {cmd!r}",
            )
        except Exception as e:
            return RunResult(returncode=-1, stdout="", stderr=str(e))


# ---------------------------------------------------------------------------
# DockerRuntime — Docker 沙箱
# ---------------------------------------------------------------------------

# 沙箱容器使用的 Docker 镜像
# 包含 Python、git、常用工具，体积合理
SANDBOX_IMAGE = "python:3.11-slim"

# 容器内 repo 的挂载路径
CONTAINER_WORKDIR = "/workspace"

# 默认资源配额：1g 内存 + 2 核（nano_cpus，1e9 = 1 核）。
# nano_cpus 用整数（cgroup v2 cpu.max 语义），不用 --cpus 浮点，避免歧义。
DEFAULT_MEM_LIMIT = "1g"
DEFAULT_NANO_CPUS = 2_000_000_000


def build_docker_run_args(
    *,
    container_name: str,
    repo_path: str,
    image: str,
    workdir: str = CONTAINER_WORKDIR,
    extra_mounts: list[tuple[str, str]] | None = None,
    readonly_root: bool = False,
    worktree_mount: tuple[str, str] | None = None,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
    network: bool = False,
) -> list[str]:
    """构造 `docker run` 的完整 argv（纯函数，零副作用，便于单测）。

    安全加固（任务规划.md Task 3.1/3.2）：
    - `--memory` / `--cpus`：硬性约束 CPU/内存，防止异常代码耗尽宿主。
      （CPU 限额以 nano_cpus 入参，1e9=1核，CLI 换算成 `--cpus` 核数。）
    - `--network none`：默认断网（network=False 时），阻断未授权外联。
    - `--read-only`：只读根文件系统（readonly_root=True 时）。
    - `--tmpfs /tmp`：始终挂载，让只读根下仍有可写临时区（apt/pip scratch）。
    - 挂载白名单：主 repo 默认 :rw；readonly_root=True 时变 :ro；worktree_mount
      始终 :rw。M4 让 worktree 挂到 workdir 即可用 rw worktree 盖住 ro 主库。

    Args:
        container_name: 容器名（--name）。
        repo_path:      宿主 repo 绝对路径，挂载到 workdir。
        image:          Docker 镜像。
        workdir:        容器内工作目录，repo 挂载目标。
        extra_mounts:   额外 bind mount [(host, container), ...]，按序追加，无模式后缀。
        readonly_root:  True → --read-only + 主 repo :ro。
        worktree_mount: (host_path, container_path)；非 None 时以 :rw 追加。
        mem_limit:      --memory 值，默认 "1g"。
        nano_cpus:      --nano-cpus 值，默认 2_000_000_000（2 核）。
        network:        True 则不注入 --network none（用默认 bridge）。

    Returns:
        完整 argv，含 image + `tail -f /dev/null` 常驻进程。
    """
    args: list[str] = [
        "docker", "run",
        "--detach",
        "--name", container_name,
        "--rm",
        "--memory", mem_limit,
        # CPU 限额：nano_cpus 是 Docker API 字段（1e9=1核），但 CLI 没有
        # --nano-cpus flag（实测报 unknown flag），只有 --cpus（decimal 核数）。
        # 这里换算后用 --cpus。
        "--cpus", str(nano_cpus / 1_000_000_000),
    ]
    if not network:
        args += ["--network", "none"]
    if readonly_root:
        args += ["--read-only"]
    args += ["--tmpfs", "/tmp"]

    # 主 repo 挂载：readonly_root 决定 ro/rw
    repo_mode = "ro" if readonly_root else "rw"
    args += ["-v", f"{repo_path}:{workdir}:{repo_mode}"]

    # 额外挂载，保留调用方顺序，无模式后缀（沿用旧行为）
    for host_path, container_path in extra_mounts or []:
        args += ["-v", f"{host_path}:{container_path}"]

    # worktree 挂载：始终 :rw（M4 用它盖住 ro 主库，实现"仅 worktree 可写"白名单）
    if worktree_mount is not None:
        host_path, container_path = worktree_mount
        args += ["-v", f"{host_path}:{container_path}:rw"]

    args += ["--workdir", workdir, image, "tail", "-f", "/dev/null"]
    return args


class DockerRuntime(Runtime):
    """
    Docker 沙箱 Runtime。

    首次调用 exec() 时懒启动容器：
    - 基于 python:3.11-slim 镜像
    - 把 repo_path bind mount 到容器的 /workspace
    - 容器持续运行（tail -f /dev/null），每条命令用 docker exec 执行
    - cleanup() 时停止并删除容器

    这样比每条命令都 docker run 快得多（避免反复启动容器的开销）。

    安全加固（任务规划.md Task 3.1/3.2，M3）：
    - 资源配额：mem_limit / nano_cpus 默认 1g / 2 核，硬性约束容器。
    - 网络：network=False（默认）→ --network none，断网。
    - 只读根 + tmpfs：readonly_root=True → 根 FS 只读，/tmp 走 tmpfs 仍可写。
    - 挂载白名单（Task 3.2）：readonly_root=True 时主 repo :ro，worktree_mount
      始终 :rw。M4 用 worktree_mount=(wt.path, /workspace) 让 rw worktree 盖住
      ro 主库，实现"仅当前任务工作区可写"。

    Args:
        repo_path:    宿主机上 repo 的绝对路径，会被 mount 进容器
        image:        Docker 镜像名，默认 python:3.11-slim
        extra_mounts: 额外的 bind mount，格式 [(host_path, container_path), ...]
        setup_cmds:   容器启动后执行的初始化命令（如 pip install -r requirements.txt）
        readonly_root: True → 根 FS 只读 + 主 repo :ro。注意：此模式下 setup_cmds 若
                       写 /workspace 或 /root/.cache 会失败；需配合 worktree_mount
                       让工作区可写，或把依赖预装进镜像。
        worktree_mount: (host_path, container_path)，始终以 :rw 挂载。M4 联动
                       WorktreeSession 时传入 (wt.path, /workspace)。
        mem_limit:    --memory 值，默认 "1g"。
        nano_cpus:    --nano-cpus 值，默认 2_000_000_000（2 核）。
        network:      True 则允许网络（不加 --network none）。默认 False 断网。
    """

    def __init__(
        self,
        repo_path: str | Path,
        image: str = SANDBOX_IMAGE,
        extra_mounts: list[tuple[str, str]] | None = None,
        setup_cmds: list[str] | None = None,
        *,
        readonly_root: bool = False,
        worktree_mount: tuple[str, str] | None = None,
        mem_limit: str = DEFAULT_MEM_LIMIT,
        nano_cpus: int = DEFAULT_NANO_CPUS,
        network: bool = False,
    ) -> None:
        self._repo_path = str(Path(repo_path).resolve())
        self._image = image
        self._extra_mounts = extra_mounts or []
        self._setup_cmds = setup_cmds or []
        self._readonly_root = readonly_root
        self._worktree_mount = worktree_mount
        self._mem_limit = mem_limit
        self._nano_cpus = nano_cpus
        self._network = network
        self._container_id: str | None = None
        # 容器名加随机后缀，避免冲突
        self._container_name = f"coding-agent-sandbox-{uuid.uuid4().hex[:8]}"

    @property
    def name(self) -> str:
        return f"docker({self._image})"

    @property
    def container_id(self) -> str | None:
        return self._container_id

    @property
    def is_running(self) -> bool:
        return self._container_id is not None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def exec(
        self,
        cmd: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> RunResult:
        """在容器里执行命令，首次调用时自动启动容器。"""
        if not self.is_running:
            startup_result = self._start_container()
            if startup_result is not None:
                # 启动失败，返回错误
                return startup_result

        # 确定容器内工作目录
        if cwd:
            # 如果 cwd 是宿主机路径，转换为容器内路径
            host_cwd = str(Path(cwd).resolve())
            if host_cwd.startswith(self._repo_path):
                relative = host_cwd[len(self._repo_path):].lstrip("/")
                container_cwd = f"{CONTAINER_WORKDIR}/{relative}" if relative else CONTAINER_WORKDIR
            else:
                container_cwd = cwd   # 可能是容器内的绝对路径
        else:
            container_cwd = CONTAINER_WORKDIR

        docker_cmd = [
            "docker", "exec",
            "--workdir", container_cwd,
            self._container_id,
            "bash", "-c", cmd,
        ]

        try:
            proc = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout + 5,   # docker exec 本身有少量开销
            )
            return RunResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s in container: {cmd!r}",
            )
        except Exception as e:
            return RunResult(returncode=-1, stdout="", stderr=str(e))

    def cleanup(self) -> None:
        """停止并删除容器。"""
        if not self._container_id:
            return
        logger.info("Stopping sandbox container %s", self._container_name)
        try:
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=15,
            )
        except Exception as e:
            logger.warning("Failed to remove container %s: %s", self._container_id, e)
        finally:
            self._container_id = None

    # ------------------------------------------------------------------
    # 内部：容器生命周期
    # ------------------------------------------------------------------

    def _start_container(self) -> RunResult | None:
        """
        拉取镜像（如需要）并启动容器。
        返回 None 表示成功，返回 RunResult 表示失败。
        """
        logger.info(
            "Starting sandbox container %s (image=%s, repo=%s)",
            self._container_name, self._image, self._repo_path,
        )

        # 检查 Docker 是否可用
        check = subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            return RunResult(
                returncode=-1,
                stdout="",
                stderr=(
                    "Docker is not available. "
                    "Make sure Docker Desktop is running, or use --no-sandbox."
                ),
            )

        # 构建 docker run 命令（资源/网络/只读根/tmpfs/挂载白名单等加固参数
        # 全部集中在 build_docker_run_args 纯函数里，便于单测）
        run_args = build_docker_run_args(
            container_name=self._container_name,
            repo_path=self._repo_path,
            image=self._image,
            workdir=CONTAINER_WORKDIR,
            extra_mounts=self._extra_mounts,
            readonly_root=self._readonly_root,
            worktree_mount=self._worktree_mount,
            mem_limit=self._mem_limit,
            nano_cpus=self._nano_cpus,
            network=self._network,
        )

        try:
            proc = subprocess.run(
                run_args,
                capture_output=True,
                text=True,
                timeout=60,  # 拉镜像可能需要时间
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                returncode=-1, stdout="",
                stderr="Timed out starting Docker container (60s). Is Docker running?",
            )

        if proc.returncode != 0:
            return RunResult(
                returncode=proc.returncode,
                stdout="",
                stderr=f"Failed to start container:\n{proc.stderr}",
            )

        self._container_id = proc.stdout.strip()
        logger.info("Container started: %s", self._container_id[:12])

        # 执行初始化命令
        for setup_cmd in self._setup_cmds:
            result = self.exec(setup_cmd, timeout=120)
            if not result.success:
                logger.warning(
                    "Setup command failed: %r\n%s", setup_cmd, result.stderr
                )

        return None   # 成功

    def install_requirements(self, requirements_file: str = "requirements.txt") -> RunResult:
        """
        在容器里安装依赖。快捷方法，等价于 exec("pip install -r requirements.txt")。
        """
        return self.exec(
            f"pip install -r {requirements_file} -q",
            timeout=120,
        )


# ---------------------------------------------------------------------------
# 便捷工厂函数
# ---------------------------------------------------------------------------

def create_runtime(
    sandbox: bool = False,
    repo_path: str | None = None,
    image: str = SANDBOX_IMAGE,
    network: bool = False,
    *,
    readonly_root: bool = False,
    worktree_mount: tuple[str, str] | None = None,
    mem_limit: str = DEFAULT_MEM_LIMIT,
    nano_cpus: int = DEFAULT_NANO_CPUS,
) -> Runtime:
    """
    根据配置创建合适的 Runtime。

    Args:
        sandbox:    True 则创建 DockerRuntime，False 则 LocalRuntime
        repo_path:  sandbox=True 时必须提供
        image:      Docker 镜像名
        network:    sandbox 模式下是否允许网络（默认 False，更安全）
        readonly_root: 透传给 DockerRuntime（根 FS 只读 + 主 repo :ro）
        worktree_mount: 透传给 DockerRuntime（始终 :rw 的 worktree 挂载）
        mem_limit:  透传给 DockerRuntime（--memory，默认 1g）
        nano_cpus:  透传给 DockerRuntime（--nano-cpus，默认 2 核）

    Returns:
        Runtime 实例
    """
    if not sandbox:
        return LocalRuntime()

    if not repo_path:
        raise ValueError("repo_path is required when sandbox=True")

    return DockerRuntime(
        repo_path=repo_path,
        image=image,
        network=network,
        readonly_root=readonly_root,
        worktree_mount=worktree_mount,
        mem_limit=mem_limit,
        nano_cpus=nano_cpus,
    )