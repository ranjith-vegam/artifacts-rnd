"""
Integration tests for MCPToolHost with a real MCP server (mcp_server.py).

The server explicitly returns CallToolResult for every tool, covering all five
content types defined in the MCP 2025-06-18 specification:

  TextContent          type="text"
  ImageContent         type="image"
  AudioContent         type="audio"
  ResourceLink         type="resource_link"
  EmbeddedResource     type="resource"  (TextResourceContents or BlobResourceContents)

Test groups
───────────────────────────────────────────────────────────────────────────────
  TestDetectTextMime       — unit: MIME heuristics
  TestShouldStoreText      — unit: storage threshold logic
  TestListTools            — server advertises all expected tools
  TestTextContent          — echo_short (inline), large report, HTML, JSON, CSV
  TestImageContent         — PNG binary round-trip
  TestAudioContent         — WAV binary round-trip
  TestResourceLink         — URI reference stays inline, never stored
  TestEmbeddedResource     — TextResourceContents (stored), BlobResourceContents (stored)
  TestMixedContent         — single CallToolResult with TextContent + ImageContent
  TestSessionAccumulation  — multiple calls accumulate correctly
"""

import re
import uuid

import httpx
import pytest

from artifact_repository import ArtifactRepository
from artifact_store import ArtifactStore
from mcp_tool_host import MCPToolHost, detect_text_mime, should_store_text


def _uid() -> str:
    return uuid.uuid4().hex


# ── helpers ────────────────────────────────────────────────────────────────

def _extract_artifact_id(summary: str) -> str:
    m = re.search(r"\[artifact:([0-9a-f]{32})\]", summary)
    assert m, f"No artifact ID in: {summary!r}"
    return m.group(1)


def _extract_all_artifact_ids(summary: str) -> list[str]:
    return re.findall(r"\[artifact:([0-9a-f]{32})\]", summary)


def _extract_link(summary: str) -> str:
    m = re.search(r"Link:\s*(https?://\S+)", summary)
    assert m, f"No link in: {summary!r}"
    return m.group(1)


# ===========================================================================
# Unit: detect_text_mime
# ===========================================================================

class TestDetectTextMime:

    def test_plain_text(self):
        assert detect_text_mime("hello world") == "text/plain"

    def test_html_doctype(self):
        assert detect_text_mime("<!DOCTYPE html><html></html>") == "text/html"

    def test_html_open_tag(self):
        assert detect_text_mime("<html lang='en'><body></body></html>") == "text/html"

    def test_html_fragment_div(self):
        assert detect_text_mime("<div class='main'>content</div>") == "text/html"

    def test_html_fragment_p_tag(self):
        assert detect_text_mime("<p>Hello world</p>") == "text/html"

    def test_html_fragment_table(self):
        assert detect_text_mime("<table><tr><td>1</td></tr></table>") == "text/html"

    def test_xml_declaration(self):
        assert detect_text_mime("<?xml version='1.0'?><root/>") == "application/xml"

    def test_valid_json_object(self):
        assert detect_text_mime('{"key": "value", "n": 1}') == "application/json"

    def test_valid_json_array(self):
        assert detect_text_mime('[1, 2, {"x": true}]') == "application/json"

    def test_invalid_json_stays_plain(self):
        assert detect_text_mime("{not: valid json}") == "text/plain"

    def test_csv_consistent_columns(self):
        csv = "id,name,score\n1,Alice,9.5\n2,Bob,8.0\n3,Carol,7.5\n"
        assert detect_text_mime(csv) == "text/csv"

    def test_csv_single_line_is_not_csv(self):
        assert detect_text_mime("id,name,score") != "text/csv"

    def test_markdown_multiple_signals(self):
        md = "# Title\n\n- item 1\n- item 2\n\n[link](http://example.com)\n"
        assert detect_text_mime(md) == "text/markdown"

    def test_markdown_single_signal_stays_plain(self):
        assert detect_text_mime("# Just a heading\nsome text") != "text/markdown"


# ===========================================================================
# Unit: should_store_text
# ===========================================================================

class TestShouldStoreText:

    T = 4096  # default threshold

    def test_html_always_stored(self):
        assert should_store_text("<p>hi</p>", "text/html", self.T) is True

    def test_json_always_stored(self):
        assert should_store_text("{}", "application/json", self.T) is True

    def test_csv_always_stored(self):
        assert should_store_text("a,b\n1,2", "text/csv", self.T) is True

    def test_markdown_always_stored(self):
        assert should_store_text("# h", "text/markdown", self.T) is True

    def test_xml_always_stored(self):
        assert should_store_text("<r/>", "application/xml", self.T) is True

    def test_short_plain_text_not_stored(self):
        assert should_store_text("Done.", "text/plain", self.T) is False

    def test_plain_text_at_threshold_stored(self):
        assert should_store_text("x" * self.T, "text/plain", self.T) is True

    def test_plain_text_below_threshold_not_stored(self):
        assert should_store_text("x" * (self.T - 1), "text/plain", self.T) is False


# ===========================================================================
# Server: tool listing
# ===========================================================================

class TestListTools:

    async def test_server_advertises_all_tools(self, tool_host: MCPToolHost):
        tools = await tool_host.list_tools()
        expected = {
            "echo_short", "get_large_report", "render_dashboard",
            "query_records", "export_csv",
            "get_pixel_image", "get_audio_clip",
            "get_resource_link", "get_embedded_text", "get_embedded_blob",
            "get_mixed_result",
        }
        assert expected.issubset(set(tools)), f"Missing: {expected - set(tools)}"


# ===========================================================================
# TextContent
# ===========================================================================

class TestTextContent:

    async def test_echo_short_is_inline_not_stored(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """Short text → CallToolResult([TextContent]) → passes inline, no artifact."""
        chat_id = _uid()
        result = await tool_host.call_tool("echo_short", {"message": "hello"}, _uid(), chat_id)

        assert "OK: hello" in result
        assert "[artifact:" not in result
        assert await repo.list_by_chat(chat_id) == []

    async def test_large_report_stored_as_text_plain(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool(
            "get_large_report", {"lines": 100}, user_id, chat_id
        )

        aid = _extract_artifact_id(result)
        record = await repo.get(aid)
        assert record["mime_type"] == "text/plain"
        assert record["tool_name"] == "get_large_report"

    async def test_large_report_link_is_downloadable(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool(
            "get_large_report", {"lines": 100}, _uid(), _uid()
        )
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert "SYSTEM REPORT" in r.text

    async def test_html_stored_as_text_html(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool("render_dashboard", {"title": "Q1"}, user_id, chat_id)

        record = await repo.get(_extract_artifact_id(result))
        assert record["mime_type"] == "text/html"

    async def test_html_link_serves_valid_document(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool("render_dashboard", {"title": "Test"}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert "<!DOCTYPE html>" in r.text
        assert "Test" in r.text

    async def test_json_stored_as_application_json(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool(
            "query_records", {"table": "orders", "limit": 60}, user_id, chat_id
        )
        record = await repo.get(_extract_artifact_id(result))
        assert record["mime_type"] == "application/json"

    async def test_json_link_returns_parseable_json(self, tool_host: MCPToolHost):
        import json as _json
        result = await tool_host.call_tool(
            "query_records", {"table": "users", "limit": 60}, _uid(), _uid()
        )
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        data = _json.loads(r.text)
        assert data["table"] == "users"
        assert len(data["records"]) == 60

    async def test_csv_stored_as_text_csv(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool("export_csv", {"rows": 60}, user_id, chat_id)
        record = await repo.get(_extract_artifact_id(result))
        assert record["mime_type"] == "text/csv"

    async def test_csv_link_has_correct_row_count(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool("export_csv", {"rows": 30}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        assert len(lines) == 31          # 1 header + 30 data rows
        assert lines[0].startswith("id,name,")


# ===========================================================================
# ImageContent
# ===========================================================================

class TestImageContent:

    async def test_image_stored_as_image_png(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """CallToolResult([ImageContent]) → always stored as image/png artifact."""
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool("get_pixel_image", {}, user_id, chat_id)

        record = await repo.get(_extract_artifact_id(result))
        assert record["mime_type"] == "image/png"
        assert record["tool_name"] == "get_pixel_image"

    async def test_image_link_returns_valid_png_magic_bytes(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool("get_pixel_image", {}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert r.content[:4] == b"\x89PNG"

    async def test_image_binary_round_trip_is_intact(self, tool_host: MCPToolHost):
        """PNG must survive base64 decode → MinIO → download without corruption."""
        result = await tool_host.call_tool("get_pixel_image", {}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            downloaded = (await c.get(_extract_link(result))).content
        assert b"IHDR" in downloaded
        assert b"IDAT" in downloaded
        assert b"IEND" in downloaded


# ===========================================================================
# AudioContent
# ===========================================================================

class TestAudioContent:

    async def test_audio_stored_as_audio_wav(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """CallToolResult([AudioContent]) → always stored as audio/wav artifact."""
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool("get_audio_clip", {}, user_id, chat_id)

        record = await repo.get(_extract_artifact_id(result))
        assert record["mime_type"] == "audio/wav"
        assert record["tool_name"] == "get_audio_clip"

    async def test_audio_link_returns_valid_wav(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool("get_audio_clip", {}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert r.content[:4] == b"RIFF"
        assert r.content[8:12] == b"WAVE"

    async def test_audio_binary_round_trip_is_intact(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool("get_audio_clip", {}, _uid(), _uid())
        async with httpx.AsyncClient() as c:
            downloaded = (await c.get(_extract_link(result))).content
        assert downloaded[:4] == b"RIFF"
        assert downloaded[8:12] == b"WAVE"
        # fmt chunk must be present
        assert b"fmt " in downloaded
        assert b"data" in downloaded


# ===========================================================================
# ResourceLink
# ===========================================================================

class TestResourceLink:

    async def test_resource_link_is_inline_not_stored(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """
        ResourceLink carries no inline data — MCPToolHost must return it as a
        reference string and must NOT create an artifact.
        """
        chat_id = _uid()
        result = await tool_host.call_tool(
            "get_resource_link", {"name": "sales-report"}, _uid(), chat_id
        )

        assert "[artifact:" not in result
        assert await repo.list_by_chat(chat_id) == []

    async def test_resource_link_summary_contains_name_and_uri(
        self, tool_host: MCPToolHost
    ):
        result = await tool_host.call_tool(
            "get_resource_link", {"name": "q1-data"}, _uid(), _uid()
        )
        assert "q1-data" in result
        assert "https://" in result or "http://" in result

    async def test_resource_link_summary_contains_mime_type(
        self, tool_host: MCPToolHost
    ):
        result = await tool_host.call_tool(
            "get_resource_link", {"name": "report"}, _uid(), _uid()
        )
        assert "application/json" in result


# ===========================================================================
# EmbeddedResource — TextResourceContents
# ===========================================================================

class TestEmbeddedTextResource:

    async def test_embedded_text_json_stored_as_application_json(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """
        EmbeddedResource(TextResourceContents, mimeType=application/json) →
        content is large JSON text → stored as application/json artifact.
        """
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool(
            "get_embedded_text", {"label": "app-config"}, user_id, chat_id
        )

        aid = _extract_artifact_id(result)
        record = await repo.get(aid)
        assert record is not None
        assert record["mime_type"] == "application/json"
        assert record["tool_name"] == "get_embedded_text"

    async def test_embedded_text_link_returns_parseable_json(self, tool_host: MCPToolHost):
        import json as _json
        result = await tool_host.call_tool(
            "get_embedded_text", {"label": "cfg"}, _uid(), _uid()
        )
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        data = _json.loads(r.text)
        assert data["label"] == "cfg"
        assert len(data["items"]) == 60


# ===========================================================================
# EmbeddedResource — BlobResourceContents
# ===========================================================================

class TestEmbeddedBlobResource:

    async def test_embedded_blob_png_stored_as_image_png(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """
        EmbeddedResource(BlobResourceContents, mimeType=image/png) →
        base64 decoded → stored as image/png artifact.
        """
        chat_id, user_id = _uid(), _uid()
        result = await tool_host.call_tool(
            "get_embedded_blob", {"label": "thumbnail"}, user_id, chat_id
        )

        aid = _extract_artifact_id(result)
        record = await repo.get(aid)
        assert record is not None
        assert record["mime_type"] == "image/png"
        assert record["tool_name"] == "get_embedded_blob"

    async def test_embedded_blob_link_returns_valid_png(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool(
            "get_embedded_blob", {"label": "icon"}, _uid(), _uid()
        )
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert r.content[:4] == b"\x89PNG"


# ===========================================================================
# Mixed: TextContent + ImageContent in one CallToolResult
# ===========================================================================

class TestMixedContent:
    """
    get_mixed_result returns CallToolResult(content=[TextContent, ImageContent]).
    The short caption stays inline; the image is always stored.
    """

    async def test_mixed_result_stores_exactly_one_artifact(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()
        await tool_host.call_tool("get_mixed_result", {"label": "revenue"}, user_id, chat_id)

        artifacts = await repo.list_by_chat(chat_id)
        assert len(artifacts) == 1
        assert artifacts[0]["mime_type"] == "image/png"

    async def test_mixed_result_caption_is_inline(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool(
            "get_mixed_result", {"label": "costs"}, _uid(), _uid()
        )
        assert "Chart label: costs" in result
        ids = _extract_all_artifact_ids(result)
        assert len(ids) == 1   # only the image

    async def test_mixed_result_image_link_serves_valid_png(self, tool_host: MCPToolHost):
        result = await tool_host.call_tool(
            "get_mixed_result", {"label": "test"}, _uid(), _uid()
        )
        async with httpx.AsyncClient() as c:
            r = await c.get(_extract_link(result))
        assert r.status_code == 200
        assert r.content[:4] == b"\x89PNG"


# ===========================================================================
# Chat accumulation
# ===========================================================================

class TestChatAccumulation:

    async def test_multiple_tool_types_accumulate_in_session(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        chat_id, user_id = _uid(), _uid()

        await tool_host.call_tool("render_dashboard", {"title": "A"}, user_id, chat_id)
        await tool_host.call_tool("query_records", {"table": "t", "limit": 60}, user_id, chat_id)
        await tool_host.call_tool("export_csv", {"rows": 60}, user_id, chat_id)
        await tool_host.call_tool("get_pixel_image", {}, user_id, chat_id)
        await tool_host.call_tool("get_audio_clip", {}, user_id, chat_id)
        await tool_host.call_tool("get_embedded_blob", {"label": "x"}, user_id, chat_id)

        artifacts = await repo.list_by_chat(chat_id)
        assert len(artifacts) == 6

        mime_types = {a["mime_type"] for a in artifacts}
        assert "text/html" in mime_types
        assert "application/json" in mime_types
        assert "text/csv" in mime_types
        assert "image/png" in mime_types
        assert "audio/wav" in mime_types

    async def test_resource_link_does_not_appear_in_session_artifacts(
        self, tool_host: MCPToolHost, repo: ArtifactRepository
    ):
        """ResourceLink creates zero artifacts; the session count must not change."""
        chat_id, user_id = _uid(), _uid()

        await tool_host.call_tool("get_resource_link", {"name": "x"}, user_id, chat_id)
        await tool_host.call_tool("echo_short", {"message": "y"}, user_id, chat_id)

        assert await repo.list_by_chat(chat_id) == []

    async def test_repeated_calls_produce_distinct_artifact_ids(
        self, tool_host: MCPToolHost
    ):
        user_id, chat_id = _uid(), _uid()
        r1 = await tool_host.call_tool("render_dashboard", {"title": "X"}, user_id, chat_id)
        r2 = await tool_host.call_tool("render_dashboard", {"title": "Y"}, user_id, chat_id)

        assert _extract_artifact_id(r1) != _extract_artifact_id(r2)
