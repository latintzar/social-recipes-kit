"""Verify that untrusted webhook input never reaches extraction."""
import hashlib
import hmac
import importlib
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from recipe_extractor.instagram_webhook import make_router, verify_signature

SECRET = "fixture-secret-for-signed-webhook-tests"
EVENT = {"entry": [{"messaging": [{"sender": {"id": "fixture-sender"}, "message": {"text": "https://www.instagram.com/reel/fixture-reel/"}}]}]}
RAW = json.dumps(EVENT).encode()

def signature(raw=RAW):
    return "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

@pytest.mark.parametrize("secret,header,status", [(None, signature(), 503), (SECRET, None, 403), (SECRET, "sha256=" + "0" * 64, 403)])
def test_rejected_delivery_never_resolves_or_extracts(monkeypatch, secret, header, status):
    if secret is None:
        monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    else:
        monkeypatch.setenv("INSTAGRAM_APP_SECRET", secret)
    module = importlib.import_module("recipe_extractor.instagram_webhook")
    extractor = importlib.import_module("recipe_extractor.extract")
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected delivery reached extraction or Graph resolution")
    monkeypatch.setattr(module, "iter_messaging_events", unexpected)
    monkeypatch.setattr(extractor, "extract_recipe", unexpected)
    app = FastAPI(); app.include_router(make_router())
    response = TestClient(app).post("/instagram/webhook", content=RAW, headers={"X-Hub-Signature-256": header} if header else {})
    assert response.status_code == status
    assert not verify_signature(header, RAW)

def test_valid_delivery_reaches_extraction_once(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", SECRET)
    module = importlib.import_module("recipe_extractor.instagram_webhook")
    extractor = importlib.import_module("recipe_extractor.extract")
    calls = []
    monkeypatch.setattr(module, "iter_messaging_events", lambda p: iter([{"sender_id": "fixture-sender", "is_echo": False, "reel_url": "https://www.instagram.com/reel/fixture-reel/"}]))
    monkeypatch.setattr(extractor, "extract_recipe", lambda url: calls.append(url) or {"status": "processed", "recipe": {"title": "Fixture recipe"}})
    app = FastAPI(); app.include_router(make_router())
    response = TestClient(app).post("/instagram/webhook", content=RAW, headers={"X-Hub-Signature-256": signature()})
    assert response.status_code == 200
    assert response.json()["handled"] == 1
    assert len(calls) == 1
    assert verify_signature(signature(), RAW)

def test_get_subscription_challenge_stays_independent(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    monkeypatch.setenv("INSTAGRAM_VERIFY_TOKEN", "fixture-verify")
    app = FastAPI(); app.include_router(make_router())
    response = TestClient(app).get("/instagram/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "fixture-verify", "hub.challenge": "fixture-challenge"})
    assert response.status_code == 200
    assert response.text == "fixture-challenge"
