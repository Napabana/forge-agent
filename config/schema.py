from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml


DEFAULT_LOG_DIR = "~/.forge-agent/logs"


class RequestTokenSpec(int):
    """兼容旧 max_tokens，同时携带已解析的请求/模型能力元数据。"""

    def __new__(cls, value: int, *, context_window: int | None, model_max_output_tokens: int | None, semantic_packet_max_tokens: int, context_budget_cap: int | None, context_safety_margin_tokens: int):
        obj = int.__new__(cls, value)
        obj.context_window = context_window
        obj.model_max_output_tokens = model_max_output_tokens
        obj.semantic_packet_max_tokens = semantic_packet_max_tokens
        obj.context_budget_cap = context_budget_cap
        obj.context_safety_margin_tokens = context_safety_margin_tokens
        return obj


class ContextBudgetSpec(int):
    """兼容旧 budget_tokens，内部只携带新的 Model-aware Budget 语义。"""

    def __new__(cls, value: int, *, model_context_window: int | None, model_max_output_tokens: int | None, request_max_output_tokens: int, context_budget_cap: int | None, safety_margin_tokens: int, model_name: str, capability_fallback: bool):
        obj = int.__new__(cls, value)
        obj.model_context_window = model_context_window
        obj.model_max_output_tokens = model_max_output_tokens
        obj.request_max_output_tokens = request_max_output_tokens
        obj.context_budget_cap = context_budget_cap
        obj.safety_margin_tokens = safety_margin_tokens
        obj.model_name = model_name
        obj.capability_fallback = capability_fallback
        return obj

    def __deepcopy__(self, memo):
        """序列化时退化为普通 int，避免 deepcopy 重建时丢失元数据参数。"""
        return int(self)


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    protocol: str = "auto"
    model: str = "claude-sonnet-4-5"
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    context_window: int | None = None
    model_max_output_tokens: int | None = None
    max_output_tokens: int = 4096
    strict_tool_schema: str = "auto"

    @property
    def request_max_output_tokens(self) -> int:
        return int(self.max_output_tokens)


@dataclass
class AgentCfg:
    max_steps: int = 40
    finish_policy: str = "balanced"
    run_profile: str = "interactive"
    multi_agent_enabled: bool = False
    max_workers: int = 3
    max_delegate_tasks: int = 3
    total_worker_tokens: int = 72_000
    worker_timeout_seconds: float = 120.0
    worker_models: dict[str, str] = field(default_factory=dict)
    max_model_calls: int | None = None
    max_total_tokens: int | None = None
    budget_tokens: int = 80_000
    context_budget_cap: int | None = None
    context_safety_margin_tokens: int = 1024
    log_dir: str = str(Path(DEFAULT_LOG_DIR).expanduser())
    planning_mode: str = "off"
    recovery_mode: str = "off"
    recovery_max_attempts: int = 4
    skills_enabled: bool = False
    skills_global_dir: str | None = "~/.forge-agent/skills"
    skills_max_loaded: int = 3
    skills_max_chars: int = 12_000
    skills_reference_max_chars: int = 8_000


@dataclass
class ShellToolConfig:
    timeout: int = 30
    max_output_tokens: int = 8_000


@dataclass
class FileToolConfig:
    max_view_lines: int = 100


@dataclass
class TestToolConfig:
    # None selects the Runtime default (sys.executable locally, python3 in Docker).
    python_executable: str | None = None


@dataclass
class ToolsConfig:
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    file: FileToolConfig = field(default_factory=FileToolConfig)
    test: TestToolConfig = field(default_factory=TestToolConfig)


@dataclass
class ContextConfig:
    repo_map_mode: str = "incremental"
    repo_map_budget: int = 12_000
    repo_map_summary_budget: int = 2_000
    compaction_mode: str = "traceable"
    compaction_threshold: float = 0.70
    compaction_target_ratio: float = 0.50
    compaction_keep_recent_tokens: int = 8_000
    # Deprecated compatibility hint. Canonical history is not trimmed by message count.
    history_window: int = 20
    semantic_packet_max_tokens: int = 16_000


@dataclass(frozen=True)
class MCPServerConfig:
    id: str
    enabled: bool = True
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    timeout_seconds: float = 30.0
    trust_read_only_annotations: bool = False
    recovery_queries: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPConfig:
    enabled: bool = False
    servers: tuple[MCPServerConfig, ...] = ()


@dataclass(frozen=True)
class MemoryConfig:
    enabled: bool = True
    root: str | None = None
    auto_capture: bool = True
    context_budget_tokens: int = 2000
    max_results: int = 5
    evaluation_enabled: bool = False
    defer_promotion: bool = False


@dataclass(frozen=True)
class DurableRuntimeConfig:
    # Enable by default only after replay and all entrypoints meet the runtime contract.
    enabled: bool = False
    root: str | None = None
    busy_timeout_ms: int = 5000
    archive_enabled: bool = True
    archive_inline_limit_bytes: int = 65_536
    archive_read_max_chars: int = 16_000


@dataclass(frozen=True)
class UIConfig:
    mode: str = "auto"
    theme: str = "default"
    show_status_line: bool = True
    show_model_metadata: bool = True
    show_token_usage: bool = True
    tool_output_mode: str = "on_error"
    tool_output_preview_lines: int = 4
    live_refresh_per_second: int = 6
    markdown_enabled: bool = True
    animations: bool = False

    def __post_init__(self):
        for name, choices in (
            ("mode", ("auto", "rich", "plain")),
            ("theme", ("default", "light", "dark", "ansi")),
            ("tool_output_mode", ("all", "on_error", "summary", "none")),
        ):
            if getattr(self, name) not in choices:
                raise ValueError(f"ui.{name} must be one of {', '.join(choices)}")
        for name in ("show_status_line", "show_model_metadata", "show_token_usage",
                     "markdown_enabled", "animations"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"ui.{name} must be a boolean")
        if type(self.tool_output_preview_lines) is not int or self.tool_output_preview_lines < 1:
            raise ValueError("ui.tool_output_preview_lines must be a positive integer")
        if type(self.live_refresh_per_second) is not int or not 1 <= self.live_refresh_per_second <= 10:
            raise ValueError("ui.live_refresh_per_second must be an integer from 1 to 10")


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    model_profiles: dict[str, LLMConfig] = field(default_factory=dict)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    durable_runtime: DurableRuntimeConfig = field(default_factory=DurableRuntimeConfig)
    cache_audit_enabled: bool = True
    cache_audit_retention_days: int = 90
    cache_audit_store_component_hashes: bool = True
    onboarding_enabled: bool = True
    onboarding_auto_start: bool = True
    workspace_trust_required: bool = True
    ui: UIConfig = field(default_factory=UIConfig)


_ENV_RE = re.compile(r"\$\{(\w+)\}")


def _load_dotenv() -> None:
    candidates: list[Path] = []
    custom = os.environ.get("FORGE_ENV_FILE")
    if custom:
        candidates.append(Path(custom))
    candidates.append(Path.home() / ".config" / "forge-agent" / "env")
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key.removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in os.environ:
                os.environ[key] = value
        break


def _expand_env(text: str) -> str:
    def replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_RE.sub(replace, text)


def _optional_positive_int(raw: dict[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None or raw[key] == "":
        return None
    if isinstance(raw[key], bool):
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw[key])
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def _context_budget_spec(*, model_name: str, context_window: int | None, model_max_output_tokens: int | None, request_max_output_tokens: int, context_budget_cap: int | None, safety_margin_tokens: int) -> ContextBudgetSpec:
    if context_window is None and context_budget_cap is None:
        raise ValueError("llm.context_window is unknown and agent.context_budget_cap is not configured")
    effective_window = context_budget_cap if context_window is None else context_window if context_budget_cap is None else min(context_window, context_budget_cap)
    assert effective_window is not None
    if request_max_output_tokens + safety_margin_tokens >= effective_window:
        raise ValueError("llm.max_output_tokens + agent.context_safety_margin_tokens must be smaller than the effective context window")
    return ContextBudgetSpec(effective_window, model_context_window=context_window, model_max_output_tokens=model_max_output_tokens, request_max_output_tokens=request_max_output_tokens, context_budget_cap=context_budget_cap, safety_margin_tokens=safety_margin_tokens, model_name=model_name, capability_fallback=context_window is None)


def _refresh_budget_bridges(config: AppConfig) -> None:
    config.agent.budget_tokens = _context_budget_spec(model_name=config.llm.model, context_window=config.llm.context_window, model_max_output_tokens=config.llm.model_max_output_tokens, request_max_output_tokens=config.llm.max_output_tokens, context_budget_cap=config.agent.context_budget_cap, safety_margin_tokens=config.agent.context_safety_margin_tokens)
    config.llm.max_tokens = RequestTokenSpec(config.llm.max_output_tokens, context_window=config.llm.context_window, model_max_output_tokens=config.llm.model_max_output_tokens, semantic_packet_max_tokens=config.context.semantic_packet_max_tokens, context_budget_cap=config.agent.context_budget_cap, context_safety_margin_tokens=config.agent.context_safety_margin_tokens)


def load_config(path: str | Path | None = None) -> AppConfig:
    _load_dotenv()
    explicit_path = path is not None
    if path is None:
        candidates = [Path.home() / ".config" / "forge-agent" / "config.yaml",
                      Path(__file__).parent / "default.yaml"]
        for p in candidates:
            if p.exists():
                path = p
                break
        else:
            return _parse({})
    # config_path = Path(path)
    config_path = Path(path).expanduser()
    if not config_path.exists():
        if explicit_path:
            raw_path = str(path)
            separator_hint = (
                " On Linux/WSL, use '/' rather than '\\' as the path separator."
                if "\\" in raw_path
                else ""
            )
            raise FileNotFoundError(
                f"Config file not found: {config_path}.{separator_hint}"
            )
        return _parse({})
    raw = _expand_env(config_path.read_text(encoding="utf-8"))
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    config = _parse(data)
    from config.credentials import PREFIX, keyring_credential

    for model in (config.llm, *config.model_profiles.values()):
        if isinstance(model.api_key, str) and model.api_key.startswith(PREFIX):
            model.api_key = keyring_credential(model.api_key) or ""
    if not explicit_path and config_path == Path.home() / ".config" / "forge-agent" / "config.yaml":
        selected_path = config_path.parent / "selected-model"
        if selected_path.exists():
            kind, _, selected = selected_path.read_text(encoding="utf-8").strip().partition(":")
            if kind == "profile" and selected in config.model_profiles:
                select_model(config, selected)
            elif kind == "model" and selected:
                merge_cli_overrides(config, model=selected)
    return config


def select_model(config: AppConfig, name: str) -> None:
    """Apply a configured model profile and refresh model-aware token budgets."""
    if name not in config.model_profiles:
        raise ValueError(f"Unknown model profile: {name}")
    config.llm = replace(config.model_profiles[name])
    _refresh_budget_bridges(config)


def save_selected_model(name: str, *, profile: bool = False) -> None:
    """Persist only a profile name, never credentials or a rewritten YAML file."""
    path = Path.home() / ".config" / "forge-agent" / "selected-model"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".selected-model-", delete=False) as temporary:
        temporary.write(("profile:" if profile else "model:") + name + "\n")
    try:
        os.replace(temporary.name, path)
    finally:
        Path(temporary.name).unlink(missing_ok=True)


def _parse(data: dict[str, Any]) -> AppConfig:
    llm_raw = data.get("llm", {})
    agent_raw = data.get("agent", {})
    durable_raw = data.get("durable_runtime", {})
    if not isinstance(durable_raw, dict) or set(durable_raw) - set(DurableRuntimeConfig.__dataclass_fields__):
        raise ValueError("invalid durable_runtime settings")
    durable_enabled = durable_raw.get("enabled", False)
    if type(durable_enabled) is not bool:
        raise ValueError("durable_runtime.enabled must be boolean")
    durable_root = durable_raw.get("root")
    if durable_root is not None and (not isinstance(durable_root, str) or not Path(durable_root).expanduser().is_absolute()):
        raise ValueError("durable_runtime.root must be an absolute path or null")
    durable_timeout = durable_raw.get("busy_timeout_ms", 5000)
    if type(durable_timeout) is not int or durable_timeout <= 0:
        raise ValueError("durable_runtime.busy_timeout_ms must be a positive integer")
    archive_enabled = durable_raw.get("archive_enabled", True)
    if type(archive_enabled) is not bool:
        raise ValueError("durable_runtime.archive_enabled must be boolean")
    archive_limit = durable_raw.get("archive_inline_limit_bytes", 65_536)
    archive_chars = durable_raw.get("archive_read_max_chars", 16_000)
    if type(archive_limit) is not int or archive_limit <= 0:
        raise ValueError("durable_runtime.archive_inline_limit_bytes must be positive")
    if type(archive_chars) is not int or not 1 <= archive_chars <= 16_000:
        raise ValueError("durable_runtime.archive_read_max_chars must be in 1..16000")
    durable_runtime = DurableRuntimeConfig(durable_enabled, durable_root, durable_timeout,
                                           archive_enabled, archive_limit, archive_chars)
    audit_raw = data.get("cache_audit", {})
    audit_flags = {"enabled", "store_component_hashes"}
    if (not isinstance(audit_raw, dict) or set(audit_raw) - (audit_flags | {"retention_days"})
            or any(type(audit_raw.get(key, True)) is not bool for key in audit_flags)):
        raise ValueError("invalid cache_audit settings")
    audit_retention = audit_raw.get("retention_days", 90)
    if type(audit_retention) is not int or audit_retention <= 0:
        raise ValueError("cache_audit.retention_days must be a positive integer")
    onboarding_raw = data.get("onboarding", {})
    onboarding_flags = {"enabled", "auto_start", "workspace_trust_required"}
    if (not isinstance(onboarding_raw, dict) or set(onboarding_raw) - (onboarding_flags | {"credential_store"})
            or any(type(onboarding_raw.get(key, True)) is not bool for key in onboarding_flags)
            or onboarding_raw.get("credential_store", "env_file") != "env_file"):
        raise ValueError("invalid onboarding settings")
    memory_raw = data.get("memory", {})
    if not isinstance(memory_raw, dict):
        raise ValueError("memory must be an object")
    unknown_memory = set(memory_raw) - {"enabled", "root", "auto_capture", "context_budget_tokens",
                                         "max_results", "evaluation_enabled", "defer_promotion"}
    if unknown_memory:
        raise ValueError(f"unknown memory setting: {sorted(unknown_memory)[0]}")
    for key in ("enabled", "auto_capture", "evaluation_enabled", "defer_promotion"):
        if type(memory_raw.get(key, getattr(MemoryConfig(), key))) is not bool:
            raise ValueError(f"memory.{key} must be boolean")
    memory_root = memory_raw.get("root")
    if memory_root is not None and (not isinstance(memory_root, str) or not memory_root.strip()):
        raise ValueError("memory.root must be a non-empty path or null")
    if memory_root is not None:
        memory_root = str(Path(memory_root).expanduser())
        if not Path(memory_root).is_absolute():
            raise ValueError("memory.root must be an absolute path")
    memory = MemoryConfig(
        enabled=memory_raw.get("enabled", True), root=memory_root,
        auto_capture=memory_raw.get("auto_capture", True),
        context_budget_tokens=_optional_positive_int(memory_raw, "context_budget_tokens") or 2000,
        max_results=_optional_positive_int(memory_raw, "max_results") or 5,
        evaluation_enabled=memory_raw.get("evaluation_enabled", False),
        defer_promotion=memory_raw.get("defer_promotion", False),
    )
    finish_policy = agent_raw.get("finish_policy", "balanced")
    if not isinstance(finish_policy, str) or finish_policy not in {"strict", "balanced", "advisory"}:
        raise ValueError("agent.finish_policy must be strict, balanced, or advisory")
    run_profile = agent_raw.get("run_profile", "interactive")
    if not isinstance(run_profile, str) or run_profile not in {"interactive", "safe", "benchmark", "sandboxed"}:
        raise ValueError("agent.run_profile must be interactive, safe, benchmark, or sandboxed")
    multi_agent_enabled = agent_raw.get("multi_agent_enabled", False)
    if type(multi_agent_enabled) is not bool:
        raise ValueError("agent.multi_agent_enabled must be boolean")
    max_workers = _optional_positive_int(agent_raw, "max_workers") or 3
    max_delegate_tasks = _optional_positive_int(agent_raw, "max_delegate_tasks") or 3
    total_worker_tokens = _optional_positive_int(agent_raw, "total_worker_tokens") or 72_000
    worker_timeout_seconds = float(agent_raw.get("worker_timeout_seconds", 120.0))
    if worker_timeout_seconds <= 0:
        raise ValueError("agent.worker_timeout_seconds must be positive")
    worker_models = agent_raw.get("worker_models", {})
    if not isinstance(worker_models, dict) or any(
        role not in {"explorer", "test_analyst", "reviewer"}
        or not isinstance(model, str) or not model.strip()
        for role, model in worker_models.items()
    ):
        raise ValueError("agent.worker_models must map supported roles to model names")
    tools_raw = data.get("tools", {})
    context_raw = data.get("context", {})
    mcp_raw = data.get("mcp", {})

    semantic_packet_max_tokens = int(context_raw.get("semantic_packet_max_tokens", 16_000))
    if semantic_packet_max_tokens <= 0:
        raise ValueError("context.semantic_packet_max_tokens must be positive")

    repo_map_mode_raw = context_raw.get("repo_map_mode", "incremental")
    if not isinstance(repo_map_mode_raw, str):
        raise ValueError("context.repo_map_mode must be one of: none, static, query_aware, incremental")
    repo_map_mode = repo_map_mode_raw.strip().lower()
    if repo_map_mode not in {"none", "static", "query_aware", "incremental"}:
        raise ValueError("context.repo_map_mode must be one of: none, static, query_aware, incremental")
    repo_map_budget = _optional_positive_int(context_raw, "repo_map_budget") or 12_000
    repo_map_summary_budget = _optional_positive_int(context_raw, "repo_map_summary_budget") or 2_000

    compaction_mode_raw = context_raw.get("compaction_mode", "traceable")
    if compaction_mode_raw is False:
        compaction_mode = "off"
    elif isinstance(compaction_mode_raw, str):
        compaction_mode = compaction_mode_raw.strip().lower()
    else:
        raise ValueError("context.compaction_mode must be one of: off, traceable")
    if compaction_mode not in {"off", "traceable"}:
        raise ValueError("context.compaction_mode must be one of: off, traceable")
    try:
        compaction_threshold = float(context_raw.get("compaction_threshold", 0.70))
        compaction_target_ratio = float(context_raw.get("compaction_target_ratio", 0.50))
    except (TypeError, ValueError) as exc:
        raise ValueError("context compaction ratios must be numeric") from exc
    if not 0 < compaction_target_ratio < compaction_threshold <= 1:
        raise ValueError(
            "context compaction ratios must satisfy "
            "0 < compaction_target_ratio < compaction_threshold <= 1"
        )
    compaction_keep_recent_tokens = (
        _optional_positive_int(context_raw, "compaction_keep_recent_tokens") or 8_000
    )

    legacy_max_tokens = _optional_positive_int(llm_raw, "max_tokens")
    max_output_tokens = _optional_positive_int(llm_raw, "max_output_tokens") or legacy_max_tokens or 4096
    context_window = _optional_positive_int(llm_raw, "context_window")
    model_max_output_tokens = _optional_positive_int(llm_raw, "model_max_output_tokens")
    if model_max_output_tokens is not None and max_output_tokens > model_max_output_tokens:
        raise ValueError("llm.max_output_tokens must be <= llm.model_max_output_tokens")
    strict_tool_schema_raw = llm_raw.get("strict_tool_schema", "auto")
    if strict_tool_schema_raw is True:
        strict_tool_schema = "on"
    elif strict_tool_schema_raw is False:
        strict_tool_schema = "off"
    elif isinstance(strict_tool_schema_raw, str):
        strict_tool_schema = strict_tool_schema_raw.strip().lower()
    else:
        raise ValueError("llm.strict_tool_schema must be one of: auto, on, off")
    if strict_tool_schema not in {"auto", "on", "off"}:
        raise ValueError("llm.strict_tool_schema must be one of: auto, on, off")

    legacy_budget = _optional_positive_int(agent_raw, "budget_tokens") if "budget_tokens" in agent_raw else 80_000
    context_budget_cap = _optional_positive_int(agent_raw, "context_budget_cap") if "context_budget_cap" in agent_raw else legacy_budget
    safety_margin = int(agent_raw.get("context_safety_margin_tokens", 1024))
    if safety_margin < 0:
        raise ValueError("agent.context_safety_margin_tokens cannot be negative")
    planning_mode_raw = agent_raw.get("planning_mode", "off")
    # PyYAML follows YAML 1.1 boolean spellings, so an unquoted `off`
    # is loaded as False. Accept that representation as the documented
    # "off" mode while still rejecting unrelated non-string values.
    if planning_mode_raw is False:
        planning_mode = "off"
    elif isinstance(planning_mode_raw, str):
        planning_mode = planning_mode_raw.strip().lower()
    else:
        raise ValueError("agent.planning_mode must be one of: off, auto, always")
    if planning_mode not in {"off", "auto", "always"}:
        raise ValueError("agent.planning_mode must be one of: off, auto, always")

    recovery_mode_raw = agent_raw.get("recovery_mode", "off")
    if recovery_mode_raw is False:
        recovery_mode = "off"
    elif isinstance(recovery_mode_raw, str):
        recovery_mode = recovery_mode_raw.strip().lower()
    else:
        raise ValueError("agent.recovery_mode must be one of: off, structured")
    if recovery_mode not in {"off", "structured"}:
        raise ValueError("agent.recovery_mode must be one of: off, structured")
    recovery_max_attempts = int(agent_raw.get("recovery_max_attempts", 4))
    if recovery_max_attempts < 1:
        raise ValueError("agent.recovery_max_attempts must be >= 1")

    skills_enabled_raw = agent_raw.get("skills_enabled", False)
    if not isinstance(skills_enabled_raw, bool):
        raise ValueError("agent.skills_enabled must be boolean")
    skills_enabled = skills_enabled_raw
    skills_global_dir_raw = agent_raw.get("skills_global_dir", "~/.forge-agent/skills")
    if skills_global_dir_raw is None:
        skills_global_dir = None
    elif isinstance(skills_global_dir_raw, str):
        skills_global_dir = skills_global_dir_raw.strip() or None
    else:
        raise ValueError("agent.skills_global_dir must be a string or null")
    skills_max_loaded = int(agent_raw.get("skills_max_loaded", 3))
    skills_max_chars = int(agent_raw.get("skills_max_chars", 12_000))
    skills_reference_max_chars = int(
        agent_raw.get("skills_reference_max_chars", 8_000)
    )
    if skills_max_loaded < 1:
        raise ValueError("agent.skills_max_loaded must be >= 1")
    if skills_max_chars < 1 or skills_reference_max_chars < 1:
        raise ValueError("agent skill context limits must be positive")

    if not isinstance(mcp_raw, dict):
        raise ValueError("mcp must be an object")
    mcp_enabled = mcp_raw.get("enabled", False)
    if not isinstance(mcp_enabled, bool):
        raise ValueError("mcp.enabled must be boolean")
    servers_raw = mcp_raw.get("servers", [])
    if not isinstance(servers_raw, list):
        raise ValueError("mcp.servers must be a list")
    server_ids: set[str] = set()
    mcp_servers: list[MCPServerConfig] = []
    for index, server_raw in enumerate(servers_raw):
        if not isinstance(server_raw, dict):
            raise ValueError(f"mcp.servers[{index}] must be an object")
        server_id = str(server_raw.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", server_id):
            raise ValueError(
                f"mcp.servers[{index}].id must match [A-Za-z][A-Za-z0-9_-]{{0,63}}"
            )
        if server_id in server_ids:
            raise ValueError(f"duplicate MCP server id: {server_id}")
        server_ids.add(server_id)
        enabled = server_raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"mcp server {server_id!r} enabled must be boolean")
        transport = str(server_raw.get("transport", "stdio")).strip().lower()
        if transport not in {"stdio", "streamable_http"}:
            raise ValueError(
                f"mcp server {server_id!r} transport must be stdio or streamable_http"
            )
        timeout_seconds = float(server_raw.get("timeout_seconds", 30.0))
        trust_read_only_annotations = server_raw.get("trust_read_only_annotations", False)
        if not isinstance(trust_read_only_annotations, bool):
            raise ValueError(
                f"mcp server {server_id!r} trust_read_only_annotations must be boolean"
            )
        recovery_queries = server_raw.get("recovery_queries", {})
        if not isinstance(recovery_queries, dict) or any(
            not isinstance(tool, str) or not tool.strip() or not isinstance(query, dict)
            or set(query) != {"query_tool", "argument"} or query.get("query_tool") == tool
            or any(not isinstance(value, str) or not value.strip() for value in query.values())
            for tool, query in recovery_queries.items()
        ):
            raise ValueError("MCP recovery_queries must map a tool to a distinct query_tool and correlation argument")
        if timeout_seconds <= 0:
            raise ValueError(f"mcp server {server_id!r} timeout_seconds must be positive")
        args_raw = server_raw.get("args", [])
        if not isinstance(args_raw, list) or any(not isinstance(v, str) for v in args_raw):
            raise ValueError(f"mcp server {server_id!r} args must be a string list")
        env_raw = server_raw.get("env", {})
        if not isinstance(env_raw, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in env_raw.items()
        ):
            raise ValueError(f"mcp server {server_id!r} env must be a string mapping")
        command_raw = server_raw.get("command")
        command = command_raw.strip() if isinstance(command_raw, str) else None
        url_raw = server_raw.get("url")
        url = url_raw.strip() if isinstance(url_raw, str) else None
        if transport == "stdio" and not command:
            raise ValueError(f"mcp stdio server {server_id!r} requires command")
        if transport == "streamable_http" and not (
            url and (url.startswith("http://") or url.startswith("https://"))
        ):
            raise ValueError(
                f"mcp streamable_http server {server_id!r} requires an http(s) url"
            )
        mcp_servers.append(
            MCPServerConfig(
                id=server_id,
                enabled=enabled,
                transport=transport,
                command=command,
                args=tuple(args_raw),
                env=dict(env_raw),
                url=url,
                timeout_seconds=timeout_seconds,
                trust_read_only_annotations=trust_read_only_annotations,
                recovery_queries=recovery_queries,
            )
        )
    mcp = MCPConfig(enabled=mcp_enabled, servers=tuple(mcp_servers))

    llm = LLMConfig(provider=llm_raw.get("provider", "anthropic"), protocol=llm_raw.get("protocol", "auto"), model=llm_raw.get("model", "claude-sonnet-4-5"), api_key=llm_raw.get("api_key", ""), base_url=llm_raw.get("base_url", "") or "", context_window=context_window, model_max_output_tokens=model_max_output_tokens, max_output_tokens=max_output_tokens, strict_tool_schema=strict_tool_schema)
    log_dir_raw = agent_raw.get("log_dir", DEFAULT_LOG_DIR)
    if not isinstance(log_dir_raw, str) or not log_dir_raw.strip():
        raise ValueError("agent.log_dir must be a non-empty string")
    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        finish_policy=finish_policy,
        run_profile=run_profile,
        multi_agent_enabled=multi_agent_enabled,
        max_workers=max_workers,
        max_delegate_tasks=max_delegate_tasks,
        total_worker_tokens=total_worker_tokens,
        worker_timeout_seconds=worker_timeout_seconds,
        worker_models=dict(worker_models),
        max_model_calls=_optional_positive_int(agent_raw, "max_model_calls"),
        max_total_tokens=_optional_positive_int(agent_raw, "max_total_tokens"),
        context_budget_cap=context_budget_cap,
        context_safety_margin_tokens=safety_margin,
        log_dir=str(Path(log_dir_raw.strip()).expanduser()),
        planning_mode=planning_mode,
        recovery_mode=recovery_mode,
        recovery_max_attempts=recovery_max_attempts,
        skills_enabled=skills_enabled,
        skills_global_dir=skills_global_dir,
        skills_max_loaded=skills_max_loaded,
        skills_max_chars=skills_max_chars,
        skills_reference_max_chars=skills_reference_max_chars,
    )
    shell_raw = tools_raw.get("shell", {})
    file_raw = tools_raw.get("file", {})
    test_raw = tools_raw.get("test", {})
    if not isinstance(test_raw, dict):
        raise ValueError("tools.test must be an object")
    python_executable_raw = test_raw.get("python_executable")
    if python_executable_raw is None:
        python_executable = None
    elif isinstance(python_executable_raw, str) and python_executable_raw.strip():
        python_executable = python_executable_raw.strip()
    else:
        raise ValueError("tools.test.python_executable must be a non-empty string or null")
    tools = ToolsConfig(
        shell=ShellToolConfig(timeout=int(shell_raw.get("timeout", 30)), max_output_tokens=int(shell_raw.get("max_output_tokens", 8_000))),
        file=FileToolConfig(max_view_lines=int(file_raw.get("max_view_lines", 100))),
        test=TestToolConfig(python_executable=python_executable),
    )
    context = ContextConfig(
        repo_map_mode=repo_map_mode,
        repo_map_budget=repo_map_budget,
        repo_map_summary_budget=repo_map_summary_budget,
        compaction_mode=compaction_mode,
        compaction_threshold=compaction_threshold,
        compaction_target_ratio=compaction_target_ratio,
        compaction_keep_recent_tokens=compaction_keep_recent_tokens,
        history_window=int(context_raw.get("history_window", 20)),
        semantic_packet_max_tokens=semantic_packet_max_tokens,
    )
    profiles_raw = data.get("models", {})
    if not isinstance(profiles_raw, dict):
        raise ValueError("models must map names to model settings")
    profiles = {"default": replace(llm)}
    allowed = {"provider", "protocol", "model", "api_key", "base_url", "max_tokens", "max_output_tokens",
               "context_window", "model_max_output_tokens", "strict_tool_schema"}
    for name, settings in profiles_raw.items():
        if not isinstance(name, str) or not name.strip() or name == "default" or not isinstance(settings, dict):
            raise ValueError("models must map non-default names to model settings")
        if not settings.get("model") or set(settings) - allowed:
            raise ValueError(f"models.{name} requires model and supported LLM settings")
        for key in ("provider", "protocol", "model", "api_key", "base_url"):
            if key in settings and not isinstance(settings[key], str):
                raise ValueError(f"models.{name}.{key} must be a string")
        for key in ("max_tokens", "max_output_tokens", "context_window", "model_max_output_tokens"):
            if key in settings:
                settings[key] = _optional_positive_int(settings, key)
                if settings[key] is None:
                    raise ValueError(f"models.{name}.{key} must be positive")
        strict = settings.get("strict_tool_schema", "auto")
        if strict not in ("auto", "on", "off", True, False):
            raise ValueError(f"models.{name}.strict_tool_schema must be auto, on, or off")
        profile = replace(llm)
        if (settings.get("provider", llm.provider) != llm.provider
                or ("base_url" in settings and settings["base_url"] != llm.base_url)):
            profile.api_key = ""
            profile.base_url = ""
        if settings["model"] != llm.model:
            profile.context_window = None
            profile.model_max_output_tokens = None
        for key, value in settings.items():
            if key != "max_tokens":
                setattr(profile, key, value)
        if "max_tokens" in settings and "max_output_tokens" not in settings:
            profile.max_output_tokens = settings["max_tokens"]
        profiles[name] = profile
    ui_raw = data.get("ui", {})
    if not isinstance(ui_raw, dict) or ui_raw.keys() - UIConfig.__dataclass_fields__.keys():
        raise ValueError("ui must be a mapping of supported terminal UI options")
    config = AppConfig(llm=llm, model_profiles=profiles, agent=agent, tools=tools, context=context, mcp=mcp, memory=memory, ui=UIConfig(**ui_raw),
                       durable_runtime=durable_runtime, cache_audit_enabled=audit_raw.get("enabled", True),
                       cache_audit_retention_days=audit_retention,
                       cache_audit_store_component_hashes=audit_raw.get("store_component_hashes", True),
                       onboarding_enabled=onboarding_raw.get("enabled", True),
                       onboarding_auto_start=onboarding_raw.get("auto_start", True),
                       workspace_trust_required=onboarding_raw.get("workspace_trust_required", True))
    _refresh_budget_bridges(config)
    return config


def merge_cli_overrides(config: AppConfig, provider: str | None = None, protocol: str | None = None, model: str | None = None, api_key: str | None = None, max_steps: int | None = None) -> AppConfig:
    if model in config.model_profiles:
        select_model(config, model)
        model = None
    if provider:
        config.llm.provider = provider
    if protocol:
        config.llm.protocol = protocol
    if model and model != config.llm.model:
        config.llm.model = model
        config.llm.context_window = None
        config.llm.model_max_output_tokens = None
        _refresh_budget_bridges(config)
    elif model:
        config.llm.model = model
    if api_key:
        config.llm.api_key = api_key
    if max_steps is not None:
        config.agent.max_steps = max_steps
    return config
