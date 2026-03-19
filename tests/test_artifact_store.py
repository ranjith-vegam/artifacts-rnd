"""
Integration tests for ArtifactStore — exercises real MinIO via testcontainers.
"""

import pytest
import httpx
from botocore.exceptions import ClientError

from artifact_store import ArtifactStore

USER_ID = "user-store-tests"
CHAT_ID = "chat-store-tests"


async def _head(store: ArtifactStore, object_key: str) -> bool:
    async with store._client() as s3:
        try:
            await s3.head_object(Bucket=store.bucket_name, Key=object_key)
            return True
        except ClientError:
            return False


class TestSave:

    async def test_save_html_returns_html_extension(self, store: ArtifactStore):
        result = await store.save(b"<h1>Hello</h1>", "text/html", USER_ID, CHAT_ID, "render_tool")
        assert result["object_key"].endswith(".html")
        assert result["mime_type"] == "text/html"

    async def test_save_json_returns_json_extension(self, store: ArtifactStore):
        result = await store.save(b'{"k":"v"}', "application/json", USER_ID, CHAT_ID, "json_tool")
        assert result["object_key"].endswith(".json")

    async def test_save_csv_returns_csv_extension(self, store: ArtifactStore):
        result = await store.save(b"a,b\n1,2", "text/csv", USER_ID, CHAT_ID, "csv_tool")
        assert result["object_key"].endswith(".csv")

    async def test_save_plain_text_returns_txt_extension(self, store: ArtifactStore):
        result = await store.save(b"plain text", "text/plain", USER_ID, CHAT_ID, "text_tool")
        assert result["object_key"].endswith(".txt")

    async def test_save_unknown_mime_returns_bin_extension(self, store: ArtifactStore):
        result = await store.save(b"\x00\x01", "application/octet-stream", USER_ID, CHAT_ID, "bin_tool")
        assert result["object_key"].endswith(".bin")

    async def test_object_key_follows_user_chat_structure(self, store: ArtifactStore):
        result = await store.save(b"data", "text/plain", "user-abc", "chat-xyz", "tool")
        parts = result["object_key"].split("/")
        assert parts[0] == "user-abc"
        assert parts[1] == "chat-xyz"

    async def test_save_uses_filename_hint_when_provided(self, store: ArtifactStore):
        result = await store.save(b"data", "text/plain", USER_ID, CHAT_ID, "tool", filename_hint="my-report.txt")
        assert result["filename_hint"] == "my-report.txt"

    async def test_save_generates_filename_hint_from_tool(self, store: ArtifactStore):
        result = await store.save(b"data", "text/plain", USER_ID, CHAT_ID, "my_tool")
        assert result["filename_hint"] == "my_tool-result.txt"

    async def test_object_is_actually_stored_in_minio(self, store: ArtifactStore):
        result = await store.save(b"<p>stored</p>", "text/html", USER_ID, CHAT_ID, "tool")
        assert await _head(store, result["object_key"]) is True

    async def test_presigned_url_is_reachable(self, store: ArtifactStore):
        payload = b"<html><body>Test</body></html>"
        result = await store.save(payload, "text/html", USER_ID, CHAT_ID, "tool")
        async with httpx.AsyncClient() as client:
            r = await client.get(result["link"])
        assert r.status_code == 200
        assert r.content == payload

    async def test_save_raises_when_data_exceeds_size_limit(self, store: ArtifactStore):
        oversized = b"x" * (store.max_size_bytes + 1)
        with pytest.raises(ValueError, match="exceeds limit"):
            await store.save(oversized, "text/plain", USER_ID, CHAT_ID, "tool")

    async def test_save_at_exact_size_limit_succeeds(self, store: ArtifactStore):
        result = await store.save(b"x" * store.max_size_bytes, "text/plain", USER_ID, CHAT_ID, "tool")
        assert result["size_bytes"] == store.max_size_bytes


class TestGetFreshLink:

    async def test_fresh_link_returns_working_url(self, store: ArtifactStore):
        payload = b"fresh link content"
        saved = await store.save(payload, "text/plain", USER_ID, CHAT_ID, "tool")
        fresh = await store.get_fresh_link(saved["object_key"])
        async with httpx.AsyncClient() as client:
            r = await client.get(fresh)
        assert r.status_code == 200
        assert r.content == payload

    async def test_fresh_link_for_missing_object_raises(self, store: ArtifactStore):
        with pytest.raises(FileNotFoundError, match="Artifact not found"):
            await store.get_fresh_link("nonexistent/user/00000000.txt")


class TestDelete:

    async def test_delete_removes_object_from_minio(self, store: ArtifactStore):
        saved = await store.save(b"delete me", "text/plain", USER_ID, CHAT_ID, "tool")
        assert await _head(store, saved["object_key"]) is True
        await store.delete(saved["object_key"])
        assert await _head(store, saved["object_key"]) is False

    async def test_delete_nonexistent_object_does_not_raise(self, store: ArtifactStore):
        await store.delete("nonexistent/key/artifact.bin")


class TestEnsureBucket:

    async def test_ensure_bucket_is_idempotent(self, store: ArtifactStore):
        await store.ensure_bucket()
        await store.ensure_bucket()

    async def test_ensure_bucket_creates_missing_bucket(self, minio_endpoint):
        fresh = ArtifactStore(minio_endpoint, "testaccesskey", "testsecretkey", "brand-new-bucket", 60)
        await fresh.ensure_bucket()
        result = await fresh.save(b"data", "text/plain", "u", "c", "tool")
        assert await _head(fresh, result["object_key"]) is True
