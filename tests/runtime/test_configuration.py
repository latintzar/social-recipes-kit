import os
from pathlib import Path

import pytest
import yaml
from social_recipes_agent.config import ConfigurationError, RuntimeConfig


def test_ambient_provider_account_is_not_configuration():
    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_environment(
            {
                "OPENROUTER_API_KEY": "ambient",
                "ANTHROPIC_API_KEY": "ambient",
                "HERMES_HOME": "/some/account",
            }
        )


def test_selected_environment_and_mcp_are_narrow(own_environment, monkeypatch):
    config = RuntimeConfig.from_environment(own_environment)
    config.prepare_state()
    env = config.child_environment(
        {
            "HOME": "/owned/system-home",
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "ambient-secret",
            "ANTHROPIC_API_KEY": "ambient-secret",
            "MEM0_API_KEY": "ambient-secret",
            "LANGFUSE_SECRET_KEY": "ambient-secret",
            "PYTHONSTARTUP": "/tmp/unsafe.py",
            "PYTHONPATH": "/tmp/unsafe",
            "HERMES_HOME": "/some/account",
        }
    )
    assert env["HOME"] == "/owned/system-home"
    assert env["OPENROUTER_API_KEY"] == "own-test-provider-key"
    assert env["HERMES_HOME"] == str(config.state / "hermes")
    assert (
        not {
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "MEM0_API_KEY",
            "LANGFUSE_SECRET_KEY",
            "PYTHONSTARTUP",
        }
        & env.keys()
    )
    assert "/tmp/unsafe" not in env["PYTHONPATH"]
    monkeypatch.setattr(os, "environ", env)
    import agent_factory

    agent_factory.ensure_hermes_config()
    saved = yaml.safe_load((config.hermes / "config.yaml").read_text())
    tools = saved["mcp_servers"]["social_recipes"]["env"]
    assert tools["RECIPE_LLM_API_KEY"] == "own-test-provider-key"
    assert (
        not {
            "SUPABASE_SERVICE_ROLE_KEY",
            "INSTAGRAM_APP_SECRET",
            "INSTAGRAM_PAGE_ACCESS_TOKEN",
            "OPENROUTER_API_KEY",
        }
        & tools.keys()
    )
    assert (config.hermes / "config.yaml").stat().st_mode & 0o777 == 0o600


def test_dedicated_state_required(own_environment, tmp_path):
    state = Path(own_environment["SOCIAL_RECIPES_STATE_DIR"])
    state.mkdir()
    (state / "private-file").write_text("unrelated")
    with pytest.raises(ConfigurationError, match="empty state"):
        RuntimeConfig.from_environment(own_environment).prepare_state()


def test_state_symlink_rejected(own_environment, tmp_path):
    target = tmp_path / "other"
    target.mkdir()
    Path(own_environment["SOCIAL_RECIPES_STATE_DIR"]).symlink_to(target)
    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_environment(own_environment).prepare_state()


def test_existing_hermes_account_not_read(own_environment):
    c = RuntimeConfig.from_environment(own_environment)
    c.prepare_state()
    (c.hermes / ".env").write_text("unrelated")
    with pytest.raises(ConfigurationError):
        c.prepare_state()


def test_anthropic_needs_explicit_compatible_vision_provider(own_environment):
    own_environment["SOCIAL_RECIPES_PROVIDER"] = "anthropic"
    with pytest.raises(ConfigurationError, match="VISION"):
        RuntimeConfig.from_environment(own_environment)
    own_environment.update(
        SOCIAL_RECIPES_VISION_BASE_URL="https://vision.example/v1",
        SOCIAL_RECIPES_VISION_MODEL="test-model",
        SOCIAL_RECIPES_VISION_API_KEY="own-vision-key",
    )
    env = RuntimeConfig.from_environment(own_environment).child_environment({})
    assert env["RECIPE_LLM_API_KEY"] == "own-vision-key"
    assert env["RECIPE_LLM_URL"] == "https://vision.example/v1/chat/completions"
    assert env["ANTHROPIC_API_KEY"] == "own-test-provider-key"


def test_dirty_sdk_and_sdk_dotenv_rejected(own_environment, monkeypatch):
    c = RuntimeConfig.from_environment(own_environment)
    c.sdk.mkdir()
    (c.sdk / "run_agent.py").write_text("")
    (c.sdk / ".env").write_text("")
    with pytest.raises(ConfigurationError):
        c.validate_sdk()
    (c.sdk / ".env").unlink()
    from types import SimpleNamespace

    monkeypatch.setattr(
        "social_recipes_agent.config.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="wrong-revision"),
    )
    with pytest.raises(ConfigurationError, match="revision"):
        c.validate_sdk()


def test_sdk_arguments_are_current_and_tools_are_scoped(own_environment, monkeypatch):
    c = RuntimeConfig.from_environment(own_environment)
    c.prepare_state()
    env = c.child_environment({})
    monkeypatch.setattr(os, "environ", env)
    from types import SimpleNamespace

    import agent_factory

    captured = {}
    monkeypatch.setattr(agent_factory, "_prepare_recipe_tools", lambda: None)
    monkeypatch.setattr(
        agent_factory,
        "_load_ai_agent",
        lambda: (
            lambda **kw: (
                captured.update(kw)
                or SimpleNamespace(
                    **kw,
                    tools=[
                        {
                            "function": {
                                "name": "mcp__social_recipes__recipes_extract_from_url"
                            }
                        }
                    ],
                )
            )
        ),
    )
    result = agent_factory.build_agent(session_id="test-thread").__dict__
    assert result["enabled_toolsets"] == ["mcp-social_recipes"]
    assert result["skip_memory"] and result["skip_background_review"]
    assert not result["load_soul_identity"] and "persist_session" not in result


def test_owned_marker_symlink_cannot_write_elsewhere(own_environment, tmp_path):
    state = Path(own_environment["SOCIAL_RECIPES_STATE_DIR"])
    state.mkdir()
    target = tmp_path / "other-marker"
    target.write_text("original")
    (state / ".social-recipes-owned").symlink_to(target)
    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_environment(own_environment).prepare_state()
    assert target.read_text() == "original"


def test_state_ancestor_alias_is_rejected(own_environment, tmp_path):
    other = tmp_path / "other-root"
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(other, target_is_directory=True)
    own_environment["SOCIAL_RECIPES_STATE_DIR"] = str(alias / "new-state")
    with pytest.raises(ConfigurationError):
        RuntimeConfig.from_environment(own_environment).prepare_state()
    assert not (other / "new-state").exists()


@pytest.mark.parametrize("filename", [".social-recipes-owned", "hermes/config.yaml"])
def test_foreign_hardlinked_state_files_are_preserved(
    own_environment, tmp_path, filename, monkeypatch
):
    config = RuntimeConfig.from_environment(own_environment)
    config.prepare_state()
    target = tmp_path / "foreign"
    target.write_text("foreign contents")
    path = config.state / filename
    path.unlink(missing_ok=True)
    os.link(target, path)
    before = target.stat().st_mode
    with pytest.raises(ConfigurationError):
        config.prepare_state()
    monkeypatch.setattr(os, "environ", config.child_environment({}))
    import agent_factory

    with pytest.raises(ConfigurationError):
        agent_factory.ensure_hermes_config()
    assert target.read_text() == "foreign contents" and target.stat().st_mode == before


def test_nonregular_marker_is_rejected(own_environment):
    config = RuntimeConfig.from_environment(own_environment)
    config.state.mkdir()
    os.mkfifo(config.state / ".social-recipes-owned")
    with pytest.raises(ConfigurationError):
        config.prepare_state()
