"""Tests for the lightweight Supabase REST wrapper."""
from __future__ import annotations

import httpx

import supabase_client as sbc
from fixtures import supabase_mock


def test_upsert_uses_postgrest_conflict_resolution(monkeypatch):
    seen: dict[str, object] = {}

    def fake_request(
        method: str,
        url: str,
        *,
        params: dict[str, str],
        json: object,
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        seen.update(
            {
                "method": method,
                "url": url,
                "params": params,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return httpx.Response(201, json=[{"id": "match-1"}])

    monkeypatch.setattr(sbc.httpx, "request", fake_request)

    client = sbc.Supabase(url="https://example.supabase.co", key="service-key")
    out = (
        client.table("user_recipe_matches")
        .upsert(
            {"device_id": "device-1", "recipe_id": "recipe-1", "status": "queued"},
            on_conflict="device_id,recipe_id",
        )
        .execute()
    )

    assert out == [{"id": "match-1"}]
    assert seen["method"] == "POST"
    assert seen["url"] == "https://example.supabase.co/rest/v1/user_recipe_matches"
    assert seen["params"] == {"on_conflict": "device_id,recipe_id"}
    assert seen["headers"]["Prefer"] == "resolution=merge-duplicates,return=representation"


def test_fake_supabase_upsert_merges_and_ignores_duplicates():
    supabase_mock.reset()
    fake = supabase_mock.FakeSupabase()

    fake.table("user_recipe_matches").upsert(
        {"device_id": "device-1", "recipe_id": "recipe-1", "status": "queued"},
        on_conflict="device_id,recipe_id",
    ).execute()
    fake.table("user_recipe_matches").upsert(
        {"device_id": "device-1", "recipe_id": "recipe-1", "status": "resolved"},
        on_conflict="device_id,recipe_id",
    ).execute()
    fake.table("user_recipe_matches").upsert(
        {"device_id": "device-1", "recipe_id": "recipe-1", "status": "queued"},
        on_conflict="device_id,recipe_id",
        ignore_duplicates=True,
    ).execute()

    rows = supabase_mock.dump("user_recipe_matches")
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"
