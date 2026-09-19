from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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

    @property
    def request_max_output_tokens(self) -> int:
        return int(self.max_output_tokens)


@dataclass
class AgentCfg:
    max_steps: int = 40
    budget_tokens: int = 80_000
    context_budget_cap: int | None = None
    context_safety_margin_tokens: int = 1024
    log_dir: str = "./logs"
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
class ToolsConfig:
    shell: ShellToolConfig = field(default_factory=ShellToolConfig)
    file: FileToolConfig = field(default_factory=FileToolConfig)


@dataclass
class ContextConfig:
    repo_map_budget: int = 8_000
    history_window: int = 20
    semantic_packet_max_tokens: int = 16_000


@dataclass
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentCfg = field(default_factory=AgentCfg)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    context: ContextConfig = field(default_factory=ContextConfig)


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
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        break


def _expand_env(text: str) -> str:
    def replace(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")
    return _ENV_RE.sub(replace, text)


def _optional_positive_int(raw: dict[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None or raw[key] == "":
        return None
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
    if path is None:
        candidates = [Path("config/default.yaml"), Path(__file__).parent / "default.yaml"]
        for p in candidates:
            if p.exists():
                path = p
                break
        else:
            return _parse({})
    config_path = Path(path)
    if not config_path.exists():
        return _parse({})
    raw = _expand_env(config_path.read_text(encoding="utf-8"))
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    return _parse(data)


def _parse(data: dict[str, Any]) -> AppConfig:
    llm_raw = data.get("llm", {})
    agent_raw = data.get("agent", {})
    tools_raw = data.get("tools", {})
    context_raw = data.get("context", {})

    semantic_packet_max_tokens = int(context_raw.get("semantic_packet_max_tokens", 16_000))
    if semantic_packet_max_tokens <= 0:
        raise ValueError("context.semantic_packet_max_tokens must be positive")

    legacy_max_tokens = _optional_positive_int(llm_raw, "max_tokens")
    max_output_tokens = _optional_positive_int(llm_raw, "max_output_tokens") or legacy_max_tokens or 4096
    context_window = _optional_positive_int(llm_raw, "context_window")
    model_max_output_tokens = _optional_positive_int(llm_raw, "model_max_output_tokens")
    if model_max_output_tokens is not None and max_output_tokens > model_max_output_tokens:
        raise ValueError("llm.max_output_tokens must be <= llm.model_max_output_tokens")

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

    llm = LLMConfig(provider=llm_raw.get("provider", "anthropic"), protocol=llm_raw.get("protocol", "auto"), model=llm_raw.get("model", "claude-sonnet-4-5"), api_key=llm_raw.get("api_key", ""), base_url=llm_raw.get("base_url", "") or "", context_window=context_window, model_max_output_tokens=model_max_output_tokens, max_output_tokens=max_output_tokens)
    agent = AgentCfg(
        max_steps=int(agent_raw.get("max_steps", 40)),
        context_budget_cap=context_budget_cap,
        context_safety_margin_tokens=safety_margin,
        log_dir=agent_raw.get("log_dir", "./logs"),
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
    tools = ToolsConfig(shell=ShellToolConfig(timeout=int(shell_raw.get("timeout", 30)), max_output_tokens=int(shell_raw.get("max_output_tokens", 8_000))), file=FileToolConfig(max_view_lines=int(file_raw.get("max_view_lines", 100))))
    context = ContextConfig(repo_map_budget=int(context_raw.get("repo_map_budget", 8_000)), history_window=int(context_raw.get("history_window", 20)), semantic_packet_max_tokens=semantic_packet_max_tokens)
    config = AppConfig(llm=llm, agent=agent, tools=tools, context=context)
    _refresh_budget_bridges(config)
    return config


def merge_cli_overrides(config: AppConfig, provider: str | None = None, protocol: str | None = None, model: str | None = None, api_key: str | None = None, max_steps: int | None = None) -> AppConfig:
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
