"""
Integration tests for ArtifactRepository — exercises real Postgres via testcontainers.

Scenarios covered:
  - save then get returns the exact same record
  - get for unknown artifact_id returns None
  - list_by_chat returns only that chat's artifacts, newest-first
  - list_by_chat for unknown chat returns empty list
  - list_by_user returns all artifacts for that user across chats
  - list_by_user for unknown user returns empty list
  - delete removes the record (get returns None afterwards)
  - delete of non-existent ID is a no-op (does not raise)
  - created_at is set automatically by Postgres
"""

import uuid

import pytest

from artifact_repository import ArtifactRepository


def _uid() -> str:
    return uuid.uuid4().hex


def _make_artifact(
    user_id: str = None,
    chat_id: str = None,
    tool_name: str = "test_tool",
    mime_type: str = "text/plain",
):
    artifact_id = _uid()
    return dict(
        artifact_id=artifact_id,
        object_key=f"{user_id or _uid()}/{chat_id or _uid()}/{artifact_id}.txt",
        mime_type=mime_type,
        filename_hint=f"{tool_name}-result.txt",
        user_id=user_id or _uid(),
        chat_id=chat_id or _uid(),
        tool_name=tool_name,
        size_bytes=42,
    )


class TestSaveAndGet:

    async def test_get_returns_exact_record_after_save(self, repo: ArtifactRepository):
        a = _make_artifact()
        await repo.save(**a)

        record = await repo.get(a["artifact_id"])

        assert record is not None
        assert record["artifact_id"] == a["artifact_id"]
        assert record["object_key"] == a["object_key"]
        assert record["mime_type"] == a["mime_type"]
        assert record["filename_hint"] == a["filename_hint"]
        assert record["user_id"] == a["user_id"]
        assert record["chat_id"] == a["chat_id"]
        assert record["tool_name"] == a["tool_name"]
        assert record["size_bytes"] == a["size_bytes"]

    async def test_get_returns_none_for_unknown_id(self, repo: ArtifactRepository):
        assert await repo.get("nonexistent-" + _uid()) is None

    async def test_created_at_is_set_automatically(self, repo: ArtifactRepository):
        a = _make_artifact()
        await repo.save(**a)
        assert (await repo.get(a["artifact_id"]))["created_at"] is not None

    async def test_tool_name_can_be_null(self, repo: ArtifactRepository):
        a = _make_artifact()
        a["tool_name"] = None
        await repo.save(**a)
        assert (await repo.get(a["artifact_id"]))["tool_name"] is None


class TestListByChat:

    async def test_returns_only_that_chats_artifacts(self, repo: ArtifactRepository):
        user = _uid()
        chat_a, chat_b = _uid(), _uid()

        a1 = _make_artifact(user_id=user, chat_id=chat_a)
        a2 = _make_artifact(user_id=user, chat_id=chat_a)
        other = _make_artifact(user_id=user, chat_id=chat_b)

        for a in (a1, a2, other):
            await repo.save(**a)

        results = await repo.list_by_chat(chat_a)
        ids = {r["artifact_id"] for r in results}

        assert a1["artifact_id"] in ids
        assert a2["artifact_id"] in ids
        assert other["artifact_id"] not in ids

    async def test_is_ordered_newest_first(self, repo: ArtifactRepository):
        chat = _uid()
        for _ in range(3):
            await repo.save(**_make_artifact(chat_id=chat))

        results = await repo.list_by_chat(chat)
        timestamps = [r["created_at"] for r in results]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_returns_empty_for_unknown_chat(self, repo: ArtifactRepository):
        assert await repo.list_by_chat("no-such-chat-" + _uid()) == []

    async def test_multiple_users_in_same_chat_both_appear(self, repo: ArtifactRepository):
        chat = _uid()
        a = _make_artifact(user_id=_uid(), chat_id=chat)
        b = _make_artifact(user_id=_uid(), chat_id=chat)
        await repo.save(**a)
        await repo.save(**b)

        ids = {r["artifact_id"] for r in await repo.list_by_chat(chat)}
        assert a["artifact_id"] in ids
        assert b["artifact_id"] in ids


class TestListByUser:

    async def test_returns_artifacts_across_chats(self, repo: ArtifactRepository):
        user = _uid()
        a1 = _make_artifact(user_id=user, chat_id=_uid())
        a2 = _make_artifact(user_id=user, chat_id=_uid())
        await repo.save(**a1)
        await repo.save(**a2)

        ids = {r["artifact_id"] for r in await repo.list_by_user(user)}
        assert a1["artifact_id"] in ids
        assert a2["artifact_id"] in ids

    async def test_does_not_include_other_users(self, repo: ArtifactRepository):
        a = _make_artifact(user_id=_uid())
        b = _make_artifact(user_id=_uid())
        await repo.save(**a)
        await repo.save(**b)

        ids = {r["artifact_id"] for r in await repo.list_by_user(a["user_id"])}
        assert a["artifact_id"] in ids
        assert b["artifact_id"] not in ids

    async def test_returns_empty_for_unknown_user(self, repo: ArtifactRepository):
        assert await repo.list_by_user("ghost-" + _uid()) == []


class TestDelete:

    async def test_delete_makes_artifact_unretrievable(self, repo: ArtifactRepository):
        a = _make_artifact()
        await repo.save(**a)
        assert await repo.get(a["artifact_id"]) is not None

        await repo.delete(a["artifact_id"])

        assert await repo.get(a["artifact_id"]) is None

    async def test_delete_removes_from_chat_listing(self, repo: ArtifactRepository):
        chat = _uid()
        a = _make_artifact(chat_id=chat)
        await repo.save(**a)
        await repo.delete(a["artifact_id"])

        ids = {r["artifact_id"] for r in await repo.list_by_chat(chat)}
        assert a["artifact_id"] not in ids

    async def test_delete_nonexistent_id_does_not_raise(self, repo: ArtifactRepository):
        await repo.delete("nonexistent-id-" + _uid())
