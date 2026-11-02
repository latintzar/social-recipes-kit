import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "original-runtime/apps/vitrina/product/backend"
for path in (
    ROOT / "runtime",
    BACKEND / "agent",
    BACKEND / "mcp",
    BACKEND / "mcp/tests",
    ROOT / "packages/recipe-extractor/src",
    *(
        ROOT / "original-runtime/packages" / part / "src"
        for part in ("core", "llm", "storage", "voice")
    ),
):
    sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline tests attempted a network call")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


@pytest.fixture
def own_environment(tmp_path):
    values = {
        "PROVIDER": "openrouter",
        "MODEL": "example/vision-model",
        "API_KEY": "own-test-provider-key",
        "STATE_DIR": str(tmp_path / "state"),
        "HERMES_SOURCE": str(tmp_path / "sdk"),
        "SUPABASE_URL": "https://own-project.example",
        "SUPABASE_SERVICE_ROLE_KEY": "own-test-service-key",
        "IG_APP_SECRET": "own-test-instagram-secret",
        "IG_VERIFY_TOKEN": "own-test-verify-token",
        "IG_ACCESS_TOKEN": "own-test-page-token",
        "IG_BUSINESS_ID": "own-test-business",
    }
    return {"SOCIAL_RECIPES_" + k: v for k, v in values.items()}


@pytest.fixture
def anyio_backend():
    return "asyncio"
