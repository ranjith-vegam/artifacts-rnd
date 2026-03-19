"""
Integration tests for the FastAPI endpoints — injects real MinIO + Postgres fixtures.

Endpoints tested:
  GET    /artifacts/{id}/link    — refresh presigned URL
  GET    /artifacts/{id}         — fetch metadata
  GET    /chats/{id}/artifacts   — list by chat
  GET    /users/{id}/artifacts   — list by user
  DELETE /artifacts/{id}         — delete artifact
"""

import uuid

import httpx
import pytest
from httpx import ASGITransport

import main as app_module
from artifact_repository import ArtifactRepository
from artifact_store import ArtifactStore
from main import app


def _uid() -> str:
    return uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _inject_deps(store: ArtifactStore, repo: ArtifactRepository):
    app_module.artifact_store = store
    app_module.artifact_repo = repo


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _store_artifact(store: ArtifactStore, repo: ArtifactRepository, **kwargs) -> dict:
    defaults = dict(
        data=b"<h1>Test</h1>",
        mime_type="text/html",
        user_id=_uid(),
        chat_id=_uid(),
        tool_name="test_tool",
        filename_hint="test.html",
    )
    defaults.update(kwargs)
    result = await store.save(**defaults)
    await repo.save(
        artifact_id=result["artifact_id"],
        object_key=result["object_key"],
        mime_type=result["mime_type"],
        filename_hint=result["filename_hint"],
        user_id=defaults["user_id"],
        chat_id=defaults["chat_id"],
        tool_name=defaults["tool_name"],
        size_bytes=result["size_bytes"],
    )
    result["user_id"] = defaults["user_id"]
    result["chat_id"] = defaults["chat_id"]
    return result


class TestGetArtifactLink:

    async def test_returns_200_with_fresh_link(self, client, store, repo):
        saved = await _store_artifact(store, repo)
        r = await client.get(f"/artifacts/{saved['artifact_id']}/link")
        assert r.status_code == 200
        body = r.json()
        assert body["artifact_id"] == saved["artifact_id"]
        assert body["link"].startswith("http")
        assert body["mime_type"] == "text/html"

    async def test_fresh_link_returns_original_content(self, client, store, repo):
        payload = b"<p>fresh link test</p>"
        saved = await _store_artifact(store, repo, data=payload)
        fresh_url = (await client.get(f"/artifacts/{saved['artifact_id']}/link")).json()["link"]
        async with httpx.AsyncClient() as direct:
            r = await direct.get(fresh_url)
        assert r.status_code == 200
        assert r.content == payload

    async def test_returns_404_for_missing_artifact(self, client):
        r = await client.get(f"/artifacts/nonexistent-{_uid()}/link")
        assert r.status_code == 404


class TestGetArtifactMetadata:

    async def test_returns_full_metadata(self, client, store, repo):
        user_id, chat_id = _uid(), _uid()
        saved = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        r = await client.get(f"/artifacts/{saved['artifact_id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == user_id
        assert body["chat_id"] == chat_id
        assert "created_at" in body

    async def test_returns_404_for_missing_artifact(self, client):
        assert (await client.get(f"/artifacts/ghost-{_uid()}")).status_code == 404


class TestListChatArtifacts:

    async def test_returns_artifacts_for_chat(self, client, store, repo):
        user_id, chat_id = _uid(), _uid()
        a1 = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        a2 = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        r = await client.get(f"/chats/{chat_id}/artifacts")
        assert r.status_code == 200
        ids = {x["artifact_id"] for x in r.json()}
        assert a1["artifact_id"] in ids
        assert a2["artifact_id"] in ids

    async def test_does_not_return_other_chats_artifacts(self, client, store, repo):
        user_id = _uid()
        chat_a, chat_b = _uid(), _uid()
        a = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_a)
        b = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_b)
        ids = {x["artifact_id"] for x in (await client.get(f"/chats/{chat_a}/artifacts")).json()}
        assert a["artifact_id"] in ids
        assert b["artifact_id"] not in ids

    async def test_returns_empty_list_for_unknown_chat(self, client):
        r = await client.get(f"/chats/ghost-{_uid()}/artifacts")
        assert r.status_code == 200
        assert r.json() == []

    async def test_results_are_ordered_newest_first(self, client, store, repo):
        user_id, chat_id = _uid(), _uid()
        for _ in range(3):
            await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        timestamps = [r["created_at"] for r in (await client.get(f"/chats/{chat_id}/artifacts")).json()]
        assert timestamps == sorted(timestamps, reverse=True)


class TestListUserArtifacts:

    async def test_returns_artifacts_across_multiple_chats(self, client, store, repo):
        user_id = _uid()
        a = await _store_artifact(store, repo, user_id=user_id, chat_id=_uid())
        b = await _store_artifact(store, repo, user_id=user_id, chat_id=_uid())
        ids = {x["artifact_id"] for x in (await client.get(f"/users/{user_id}/artifacts")).json()}
        assert a["artifact_id"] in ids
        assert b["artifact_id"] in ids

    async def test_does_not_return_other_users_artifacts(self, client, store, repo):
        a = await _store_artifact(store, repo, user_id=_uid())
        b = await _store_artifact(store, repo, user_id=_uid())
        ids = {x["artifact_id"] for x in (await client.get(f"/users/{a['user_id']}/artifacts")).json()}
        assert a["artifact_id"] in ids
        assert b["artifact_id"] not in ids

    async def test_returns_empty_list_for_unknown_user(self, client):
        r = await client.get(f"/users/ghost-{_uid()}/artifacts")
        assert r.status_code == 200
        assert r.json() == []


class TestDeleteArtifact:

    async def test_delete_returns_deleted_id(self, client, store, repo):
        saved = await _store_artifact(store, repo)
        r = await client.delete(f"/artifacts/{saved['artifact_id']}")
        assert r.status_code == 200
        assert r.json() == {"deleted": saved["artifact_id"]}

    async def test_deleted_artifact_is_no_longer_retrievable(self, client, store, repo):
        saved = await _store_artifact(store, repo)
        await client.delete(f"/artifacts/{saved['artifact_id']}")
        assert (await client.get(f"/artifacts/{saved['artifact_id']}")).status_code == 404

    async def test_deleted_artifact_gone_from_chat_list(self, client, store, repo):
        user_id, chat_id = _uid(), _uid()
        a = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        b = await _store_artifact(store, repo, user_id=user_id, chat_id=chat_id)
        await client.delete(f"/artifacts/{a['artifact_id']}")
        ids = {x["artifact_id"] for x in (await client.get(f"/chats/{chat_id}/artifacts")).json()}
        assert a["artifact_id"] not in ids
        assert b["artifact_id"] in ids

    async def test_delete_returns_404_for_missing_artifact(self, client):
        r = await client.delete(f"/artifacts/ghost-{_uid()}")
        assert r.status_code == 404

    async def test_presigned_link_broken_after_delete(self, client, store, repo):
        saved = await _store_artifact(store, repo, data=b"<p>delete me</p>")
        await client.delete(f"/artifacts/{saved['artifact_id']}")
        async with httpx.AsyncClient() as direct:
            r = await direct.get(saved["link"])
        assert r.status_code in (403, 404)
