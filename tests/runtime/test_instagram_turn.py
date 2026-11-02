from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time

import httpx
import pytest
from social_recipes_agent import mcp_server
from social_recipes_agent.config import RuntimeConfig


@pytest.fixture
def runtime(own_environment, monkeypatch):
    config = RuntimeConfig.from_environment(own_environment)
    config.prepare_state()
    monkeypatch.setattr(os, "environ", config.child_environment({}))
    import chat_threads
    import session_source
    from fixtures import supabase_mock
    from platforms import instagram_dm as ig
    from social_recipes_agent.worker import create_app

    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()
    monkeypatch.setattr(chat_threads, "_get_supabase", lambda: fake)
    monkeypatch.setattr(session_source, "_load_chat_threads", lambda: chat_threads)
    from platforms import ig_webhook_inbound

    monkeypatch.setattr(
        ig_webhook_inbound, "resolve_ig_share_url_from_message_mid", lambda mid: None
    )
    monkeypatch.setattr(ig, "send_sender_action", lambda *args: None)

    # Profile and commerce work are intentionally outside this recipe-only path.
    def commerce_called(*args, **kwargs):
        raise AssertionError("Recipe profile reached commerce/profile provider")

    for name in (
        "_sync_instagram_profile_metadata",
        "_send_proactive_location_link",
        "_load_staged_recipe",
        "_load_thread_user_location",
    ):
        monkeypatch.setattr(ig, name, commerce_called)
    replies = []
    done = threading.Event()

    def send(recipient, text):
        replies.append((recipient, text))
        done.set()
        return {"recipient_id": recipient, "message_id": "test-reply"}

    monkeypatch.setattr(ig, "send_message", send)
    ig._SEEN_MIDS.clear()
    # Use the real burst queue and timer, with a shorter quiet period for offline QA.
    monkeypatch.setattr(ig, "_IG_BURST_QUIET_S", 0.01)
    yield config, create_app(), ig, chat_threads, supabase_mock, replies, done
    for timer in list(ig._IG_BURST_TIMERS.values()):
        timer.cancel()
    ig._IG_BURST_TIMERS.clear()
    ig._IG_BURST_BUFFERS.clear()


def payload(
    sender="sender-a", mid="message-a", text="https://www.instagram.com/reel/EXAMPLE/"
):
    return {
        "object": "instagram",
        "entry": [
            {
                "id": "own-test-business",
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": "own-test-business"},
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ],
    }


async def post(app, body, secret="own-test-instagram-secret", signature=True):
    raw = json.dumps(body).encode()
    headers = (
        {
            "x-hub-signature-256": "sha256="
            + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        }
        if signature
        else {}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/v1/instagram/webhook", content=raw, headers=headers)


@pytest.mark.anyio
async def test_signed_webhook_original_queue_session_tool_reply(runtime, monkeypatch):
    config, app, ig, ct, store, replies, done = runtime
    import agent_factory

    monkeypatch.setattr(agent_factory, "_prepare_recipe_tools", lambda: None)
    calls = []

    def extraction(url, **kw):
        calls.append((url, kw))
        return {
            "status": "processed",
            "recipe": {
                "title": "Test tomato pasta",
                "ingredients": [{"name": "Tomatoes", "amount": "2"}],
                "steps": ["Cook the pasta."],
            },
        }

    monkeypatch.setattr("recipe_extractor.extract.extract_recipe", extraction)

    class Agent:
        def __init__(self, **kw):
            self.kw = kw
            self.tools = [
                {"function": {"name": "mcp__social_recipes__recipes_extract_from_url"}}
            ]

        def close(self):
            pass

        def run_conversation(self, **turn):
            assert "location" not in self.ephemeral_system_prompt.lower()
            assert (
                "basket" not in turn["system_message"].lower()
                or "Vitrina" in turn["system_message"]
            )
            assert "https://www.instagram.com/reel/EXAMPLE/" in turn["user_message"]
            result = mcp_server.extract_from_url(
                "https://www.instagram.com/reel/EXAMPLE/"
            )
            self.kw["tool_start_callback"](
                "recipes_extract_from_url",
                "test-call",
                {"url": "https://www.instagram.com/reel/EXAMPLE/"},
            )
            self.kw["tool_complete_callback"](
                "recipes_extract_from_url", "test-call", {}, result
            )
            self.kw["stream_delta_callback"](
                "Test tomato pasta\n2 tomatoes\nCook the pasta."
            )

    monkeypatch.setattr(agent_factory, "_load_ai_agent", lambda: Agent)
    response = await post(app, payload())
    assert response.status_code == 200 and response.json()["handled"] == 1
    assert done.wait(3), "Actual queued Hermes turn produced no reply"
    for _ in range(100):
        if len(store.dump("chat_messages")) == 2:
            break
        time.sleep(0.01)
    assert calls == [
        (
            "https://www.instagram.com/reel/EXAMPLE/",
            {"cookies_from_browser": "", "persist": True},
        )
    ]
    assert (
        replies and replies[0][0] == "sender-a" and "Test tomato pasta" in replies[0][1]
    )
    rows = store.dump("chat_messages")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    tid = ig.ig_thread_id("sender-a")
    assert len(ct.list_messages(tid, cart_key="ig:sender-a")) == 2
    with pytest.raises(ct.ThreadAccessDenied):
        ct.list_messages(tid, cart_key="ig:sender-b")
    with pytest.raises(ct.ThreadAccessDenied):
        ct.append_message(tid, "assistant", "bad", cart_key="ig:sender-b")
    assert len(store.dump("chat_messages")) == 2
    response = await post(app, payload())
    assert response.json()["skipped"] == 1 and len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "signature,secret", [(False, "own-test-instagram-secret"), (True, "wrong-secret")]
)
async def test_bad_signature_never_enqueues(runtime, monkeypatch, signature, secret):
    config, app, ig, ct, store, replies, done = runtime
    monkeypatch.setattr(
        ig,
        "_ig_burst_enqueue",
        lambda msg: pytest.fail("Unauthenticated queue admission"),
    )
    response = await post(app, payload(), signature=signature, secret=secret)
    assert (
        response.status_code == 403 and not replies and not store.dump("chat_messages")
    )


@pytest.mark.anyio
async def test_missing_secret_fails_closed(runtime, monkeypatch):
    _, app, ig, _, _, replies, _ = runtime
    monkeypatch.delenv("INSTAGRAM_APP_SECRET")
    assert (await post(app, payload())).status_code == 403
    assert not replies


@pytest.mark.anyio
async def test_verification_and_body_limit(runtime):
    _, app, _, _, _, _, _ = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        good = await client.get(
            "/v1/instagram/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "own-test-verify-token",
                "hub.challenge": "test-challenge",
            },
        )
        bad = await client.get(
            "/v1/instagram/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "test-challenge",
            },
        )
        huge = await client.post("/v1/instagram/webhook", content=b"x" * 1_048_577)
        assert good.status_code == 200 and good.text == "test-challenge"
        assert bad.status_code == 403 and huge.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/a",
        "https://localhost/reel/a",
        "https://instagram.com.evil.test/reel/a",
        "https://www.instagram.com@evil.test/reel/a",
        "https://lookaside.fbsbx.com/other",
        "https://www.instagram.com:444/reel/a",
    ],
)
def test_tool_rejects_non_social_fetch_targets(url):
    with pytest.raises(ValueError):
        mcp_server.validate_url(url)


@pytest.mark.anyio
async def test_foreign_account_and_echo_do_not_enqueue(runtime, monkeypatch):
    _, app, ig, _, store, replies, _ = runtime
    monkeypatch.setattr(
        ig, "_ig_burst_enqueue", lambda msg: pytest.fail("Foreign/echo queue admission")
    )
    foreign = payload()
    foreign["entry"][0]["id"] = "another-business"
    assert (await post(app, foreign)).json()["handled"] == 0
    echo = payload()
    echo["entry"][0]["messaging"][0]["message"]["is_echo"] = True
    assert (await post(app, echo)).json()["skipped"] == 1
    assert not replies and not store.dump("chat_messages")


def test_history_and_access_failure_use_original_session_handlers(runtime):
    _, _, ig, ct, store, _, _ = runtime
    import session_source

    a = ig.ig_thread_id("history-a")
    b = ig.ig_thread_id("history-b")
    ct.create_thread(id=a, cart_key="ig:history-a")
    ct.create_thread(id=b, cart_key="ig:history-b")
    session_source.persist_user_turn(
        thread_id=a, user_id=None, cart_key="ig:history-a", text="A recipe question"
    )
    session_source.persist_assistant_turn(
        thread_id=a, user_id=None, cart_key="ig:history-a", text="A recipe answer"
    )
    session_source.persist_user_turn(
        thread_id=b, user_id=None, cart_key="ig:history-b", text="B recipe question"
    )
    assert session_source.load_history(
        thread_id=a, user_id=None, cart_key="ig:history-a"
    ) == [
        {"role": "user", "content": "A recipe question"},
        {"role": "assistant", "content": "A recipe answer"},
    ]
    assert session_source.load_history(
        thread_id=b, user_id=None, cart_key="ig:history-b"
    ) == [{"role": "user", "content": "B recipe question"}]
    with pytest.raises(ct.ThreadAccessDenied):
        session_source.load_history(thread_id=a, user_id=None, cart_key="ig:history-b")
    with pytest.raises(ct.ThreadAccessDenied):
        session_source.persist_user_turn(
            thread_id=a, user_id=None, cart_key="ig:history-b", text="wrong sender"
        )


@pytest.mark.parametrize(
    "header",
    [
        "sha256=é",
        "sha256=" + ("é" * 64),
        "sha256=" + ("g" * 64),
        "sha256=" + ("0" * 63),
        "sha256=" + ("0" * 65),
        "sha256= " + ("0" * 64),
        "sha1=" + ("0" * 64),
        "",
    ],
)
def test_malformed_signature_is_false_not_an_exception(runtime, header):
    _, _, ig, _, _, _, _ = runtime
    assert ig._verify_signature(header, b"{}") is False
