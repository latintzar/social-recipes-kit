"""Recovered Vitrina turn factory, adapted for the Social Recipes launcher.

The original shared callback/turn surface is retained. The public entry selects
its own provider, pinned Hermes source, private state and recipe MCP toolset.
See docs/original-runtime.md and the per-file lineage manifest for adaptations.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger(__name__)

_DEFAULT_MAX_ITERATIONS = 12
_DEFAULT_MAX_TOKENS = 4096
_MCP_SERVER_CONTAINER_PATH = "/app/mcp/server.py"
_MCP_PYTHONPATH_CONTAINER = (
    "/app/mcp:/app/recipe_videos:/app/vitrina_agent:/app:"
    "/app/packages/core/src:/app/packages/llm/src:/app/packages/storage/src"
)
_MCP_ENV_BLACKLIST = frozenset({"PYTHONPATH"})
_VALID_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HERMES_CONFIG_BOOTSTRAPPED = False


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    api_key: str
    base_url: str
    model: str


def _to_anthropic_model(model: str) -> str:
    if model.startswith("anthropic/"):
        model = model.split("/", 1)[1]
    return re.sub(r"claude-(\w+)-(\d+)\.(\d+)", r"claude-\1-\2-\3", model)


def _resolve_provider() -> ProviderProfile:
    """Resolve only configuration admitted by the public project launcher."""
    from social_recipes_agent.config import RuntimeConfig
    config = RuntimeConfig.from_environment()
    provider = config.values["PROVIDER"]
    return ProviderProfile(provider, config.values["API_KEY"],
        "https://api.anthropic.com" if provider == "anthropic" else "https://openrouter.ai/api/v1",
        _to_anthropic_model(config.values["MODEL"]) if provider == "anthropic" else config.values["MODEL"])


def _resolve_mcp_command() -> tuple[str, list[str], str]:
    return sys.executable, ["-m", "social_recipes_agent.mcp_server"], os.environ["PYTHONPATH"]


def _build_mcp_stdio_block() -> str:
    import yaml
    from social_recipes_agent.config import RuntimeConfig
    config = RuntimeConfig.from_environment()
    env = config.child_environment()
    # The recipe tool needs its own vision credentials, not IG or session-store keys.
    allowed = {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "RECIPE_OUTPUT_DIR", "RECIPE_LLM_URL", "RECIPE_LLM_API_KEY", "RECIPE_MODEL", "OTEL_SDK_DISABLED", "DO_NOT_TRACK"}
    env = {key: value for key, value in env.items() if key in allowed}
    block = {"social_recipes": {"command": sys.executable,
        "args": ["-m", "social_recipes_agent.mcp_server"], "env": env,
        "tools": {"include": ["recipes_extract_from_url"], "resources": False, "prompts": False},
        "timeout": 120, "connect_timeout": 30}}
    return yaml.safe_dump(block, sort_keys=True)


def ensure_hermes_config() -> None:
    """Create config inside the explicit recipient-owned project state only."""
    import yaml
    from social_recipes_agent.config import RuntimeConfig, ConfigurationError, require_private_regular_file
    config = RuntimeConfig.from_environment()
    home = config.hermes
    if os.environ.get("SOCIAL_RECIPES_RUNTIME") != "1" or os.environ.get("HERMES_HOME") != str(home):
        raise ConfigurationError("Start with social-recipes-agent serve to isolate project configuration")
    if not (config.state / ".social-recipes-owned").is_file():
        raise ConfigurationError("Initialize your project state through the launcher")
    cfg_path = home / "config.yaml"
    require_private_regular_file(config.state / ".social-recipes-owned")
    require_private_regular_file(cfg_path)
    desired = {"memory": {"provider": ""}, "tools": {"tool_search": {"enabled": "off"}}, "mcp_servers": yaml.safe_load(_build_mcp_stdio_block())}
    cfg_path.write_text(yaml.safe_dump(desired, sort_keys=True), encoding="utf-8")
    cfg_path.chmod(0o600)


def _load_ai_agent() -> Any:
    """Load the pinned SDK only; installed Hermes accounts are never searched."""
    import importlib
    from social_recipes_agent.config import RuntimeConfig, ConfigurationError
    config = RuntimeConfig.from_environment()
    expected = config.sdk / "run_agent.py"
    module = importlib.import_module("run_agent")
    if Path(module.__file__).resolve() != expected:
        raise ConfigurationError("Hermes import must resolve to your pinned checkout")
    return module.AIAgent


def _prepare_recipe_tools() -> None:
    """The pinned SDK requires explicit MCP discovery in programmatic entry points."""
    from tools.mcp_tool_discovery import discover_mcp_tools
    names = discover_mcp_tools(allowed_mcp_names=["social_recipes"])
    expected = "mcp__social_recipes__recipes_extract_from_url"
    if expected not in names:
        raise RuntimeError("Start the recipe MCP tool with your configured Python environment")


def build_agent(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    stream_delta_callback: Callable[[str | None], None] | None = None,
    tool_start_callback: Callable[[str, str, Any], None] | None = None,
    tool_complete_callback: Callable[[str, str, Any, Any], None] | None = None,
    thinking_callback: Callable[[str], None] | None = None,
    reasoning_callback: Callable[[str], None] | None = None,
    step_callback: Callable[..., None] | None = None,
    clarify_callback: Callable[..., Any] | None = None,
    tool_progress_callback: Callable[..., None] | None = None,
    status_callback: Callable[..., None] | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    max_tokens: int | None = None,
) -> Any | None:
    """Construct Hermes with this project's configured provider and recipe tools."""
    profile = _resolve_provider()

    ensure_hermes_config()
    AIAgent = _load_ai_agent()
    _prepare_recipe_tools()

    agent = AIAgent(
        base_url=profile.base_url,
        api_key=profile.api_key,
        provider=profile.name,
        model=profile.model,
        max_iterations=max_iterations,
        max_tokens=max_tokens
        or int(os.getenv("VITRINA_AGENT_MAX_TOKENS") or _DEFAULT_MAX_TOKENS),
        enabled_toolsets=["mcp-social_recipes"],
        disabled_toolsets=[
            "terminal",
            "files",
            "web",
            "web_search",
            "browser",
            "code_execution",
            "delegate",
            "memory",
            "skills",
            "agent_management",
            "todo",
            "vision",
        ],
        quiet_mode=True,
        save_trajectories=False,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        load_soul_identity=False,
        session_id=session_id,
        user_id=user_id,
        platform="instagram_dm",
        stream_delta_callback=stream_delta_callback,
        tool_start_callback=tool_start_callback,
        tool_complete_callback=tool_complete_callback,
        thinking_callback=thinking_callback,
        reasoning_callback=reasoning_callback,
        step_callback=step_callback,
        clarify_callback=clarify_callback,
        tool_progress_callback=tool_progress_callback,
        status_callback=status_callback,
    )
    names = {tool.get("function", {}).get("name") for tool in agent.tools}
    if names != {"mcp__social_recipes__recipes_extract_from_url"}:
        agent.close()
        raise RuntimeError("The recipe profile requires its single configured extraction tool")
    return agent


def stub_reply(user_text: str) -> str:
    return (
        f"(stub) You said: {user_text!r}. Set ANTHROPIC_API_KEY or "
        "OPENROUTER_API_KEY to enable live Vitrina shopper responses."
    )
