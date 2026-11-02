import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from social_recipes_agent.config import RuntimeConfig


def test_pinned_hermes_real_stdio_tool_surface(own_environment):
    source = os.environ.get("SOCIAL_RECIPES_TEST_HERMES_SOURCE")
    if not source:
        pytest.skip(
            "Set SOCIAL_RECIPES_TEST_HERMES_SOURCE to the dedicated pinned SDK checkout"
        )
    own_environment["SOCIAL_RECIPES_HERMES_SOURCE"] = source
    config = RuntimeConfig.from_environment(own_environment)
    config.validate_sdk()
    config.prepare_state()
    probe = Path(__file__).with_name("sdk_probe.py")
    result = subprocess.run(
        [sys.executable, str(probe)],
        env=config.child_environment(),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, (result.stdout + "\n" + result.stderr)[-4000:]
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt["sdk_import"] and not receipt["external_network_succeeded"]
    assert receipt["tools"] == ["mcp__social_recipes__recipes_extract_from_url"]
