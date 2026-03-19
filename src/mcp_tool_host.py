"""
MCPToolHost: connects to an MCP server, calls tools, and routes every
CallToolResult content item through the artifact pipeline.

All five MCP content types (spec 2025-06-18)
─────────────────────────────────────────────
  TextContent      type="text"          → MIME-detected; stored when warrants it
  ImageContent     type="image"         → always stored (binary, base64 on wire)
  AudioContent     type="audio"         → always stored (binary, base64 on wire)
  ResourceLink     type="resource_link" → URI reference; returned inline (no data)
  EmbeddedResource type="resource"      → delegates:
      TextResourceContents  → handled like TextContent
      BlobResourceContents  → decoded and handled like ImageContent/AudioContent

Storage decision for text
──────────────────────────
  • text/html, application/xml   → always store (browser-renderable)
  • application/json, text/csv   → always store (structured data)
  • text/markdown                → always store (has formatting structure)
  • text/plain                   → store only when ≥ artifact_size_threshold bytes
  Binary content types           → always store
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)

from artifact_repository import ArtifactRepository
from artifact_store import ArtifactStore

# Default: store plain text once it exceeds 4 KB
_DEFAULT_TEXT_THRESHOLD = 4 * 1024


# ---------------------------------------------------------------------------
# Module-level MIME helpers (pure functions, easy to unit-test independently)
# ---------------------------------------------------------------------------

def detect_text_mime(text: str) -> str:
    """Infer MIME type from text content using structural heuristics."""
    s = text.strip()

    # XML declaration
    if s.startswith("<?xml"):
        return "application/xml"

    # Full HTML document
    if re.match(r"<!DOCTYPE\s+html", s, re.IGNORECASE):
        return "text/html"
    if re.match(r"<html[\s>]", s, re.IGNORECASE):
        return "text/html"

    # HTML fragment — common block/inline tags at the start
    _HTML_TAG = re.compile(
        r"^<(div|span|p|h[1-6]|ul|ol|li|table|tr|td|th|form|input|"
        r"a|img|script|style|head|body|header|footer|nav|section|"
        r"article|main|button|select|textarea)\b",
        re.IGNORECASE,
    )
    if _HTML_TAG.match(s):
        return "text/html"

    # Valid JSON object or array
    if s[:1] in ("{", "["):
        try:
            json.loads(s)
            return "application/json"
        except json.JSONDecodeError:
            pass

    # CSV heuristic: ≥ 2 lines, consistent comma columns, first row looks like headers
    if _looks_like_csv(s):
        return "text/csv"

    # Markdown: needs at least two structural signals to avoid false-positives
    if _looks_like_markdown(s):
        return "text/markdown"

    return "text/plain"


def should_store_text(text: str, mime_type: str, threshold_bytes: int) -> bool:
    """Return True when a text result should be offloaded to MinIO."""
    # Always store structured / renderable content regardless of size
    if mime_type in (
        "text/html",
        "application/xml",
        "text/xml",
        "application/json",
        "text/csv",
        "text/markdown",
    ):
        return True

    # Plain text: only store if it exceeds the size threshold
    return len(text.encode("utf-8")) >= threshold_bytes


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _looks_like_csv(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    sample = lines[:10]
    counts = [ln.count(",") for ln in sample]
    if counts[0] == 0:
        return False
    modal = max(set(counts), key=counts.count)
    # ≥ 80 % of sampled lines share the same comma count
    return sum(1 for c in counts if c == modal) / len(counts) >= 0.8


def _looks_like_markdown(text: str) -> bool:
    signals = [
        re.compile(r"^#{1,6}\s", re.MULTILINE),      # ATX headings
        re.compile(r"^[-*+]\s", re.MULTILINE),         # Bullet lists
        re.compile(r"^\d+\.\s", re.MULTILINE),         # Ordered lists
        re.compile(r"`{1,3}"),                          # Inline/fenced code
        re.compile(r"\[.+?\]\(.+?\)"),                  # Links
        re.compile(r"^>{1}\s", re.MULTILINE),           # Block quotes
    ]
    return sum(1 for p in signals if p.search(text)) >= 2


# ---------------------------------------------------------------------------
# MCPToolHost
# ---------------------------------------------------------------------------

class MCPToolHost:
    """
    Wraps an MCP ClientSession and routes tool results through the artifact
    pipeline.  Use as an async context manager:

        async with MCPToolHost(store, repo, server_params) as host:
            summary = await host.call_tool("render_dashboard", {"title": "Q1"}, uid, sid)
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        artifact_repo: ArtifactRepository,
        server_params: StdioServerParameters,
        artifact_size_threshold: int = _DEFAULT_TEXT_THRESHOLD,
    ):
        self.artifact_store = artifact_store
        self.artifact_repo = artifact_repo
        self.server_params = server_params
        self.artifact_size_threshold = artifact_size_threshold

        self._stdio_cm: Any = None
        self._session: ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._stdio_cm = stdio_client(self.server_params)
        read, write = await self._stdio_cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(None, None, None)
            self._stdio_cm = None

    async def __aenter__(self) -> "MCPToolHost":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[str]:
        """Return the names of all tools advertised by the MCP server."""
        result = await self._session.list_tools()
        return [t.name for t in result.tools]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        user_id: str,
        chat_id: str,
    ) -> str:
        """
        Call *tool_name* on the MCP server and return a compact summary string
        suitable for an LLM context window.

        Large / binary results are stored in MinIO and Postgres; only their
        artifact IDs and pre-signed links are included in the returned string.
        """
        result: CallToolResult = await self._session.call_tool(tool_name, arguments)

        if result.isError:
            error_text = " ".join(
                item.text for item in result.content if isinstance(item, TextContent)
            )
            return f"[ERROR] Tool '{tool_name}' failed: {error_text}"

        parts: list[str] = []
        for item in result.content:
            parts.append(
                await self._dispatch(item, tool_name, user_id, chat_id)
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Content-type dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        content: Any,
        tool_name: str,
        user_id: str,
        chat_id: str,
    ) -> str:
        if isinstance(content, TextContent):
            return await self._handle_text(content.text, tool_name, user_id, chat_id)

        if isinstance(content, ImageContent):
            return await self._handle_binary(
                base64.b64decode(content.data),
                content.mimeType,
                tool_name, user_id, chat_id,
            )

        if isinstance(content, AudioContent):
            return await self._handle_binary(
                base64.b64decode(content.data),
                content.mimeType,
                tool_name, user_id, chat_id,
            )

        if isinstance(content, ResourceLink):
            return self._handle_resource_link(content)

        if isinstance(content, EmbeddedResource):
            return await self._handle_embedded(content, tool_name, user_id, chat_id)

        # Fallback: treat unknown content as plain text if it has a .text attr
        text = getattr(content, "text", None)
        if text is not None:
            return await self._handle_text(str(text), tool_name, user_id, chat_id)

        return f"[unsupported content type: {type(content).__name__}]"

    # ------------------------------------------------------------------
    # Per-type handlers
    # ------------------------------------------------------------------

    async def _handle_text(
        self, text: str, tool_name: str, user_id: str, chat_id: str
    ) -> str:
        mime = detect_text_mime(text)
        if not should_store_text(text, mime, self.artifact_size_threshold):
            return text
        return await self._store_and_summarize(
            text.encode("utf-8"), mime, tool_name, user_id, chat_id
        )

    async def _handle_binary(
        self,
        data: bytes,
        mime_type: str,
        tool_name: str,
        user_id: str,
        chat_id: str,
    ) -> str:
        return await self._store_and_summarize(
            data, mime_type, tool_name, user_id, chat_id
        )

    async def _handle_embedded(
        self,
        resource: EmbeddedResource,
        tool_name: str,
        user_id: str,
        chat_id: str,
    ) -> str:
        res = resource.resource
        if hasattr(res, "text") and res.text is not None:
            return await self._handle_text(res.text, tool_name, user_id, chat_id)
        if hasattr(res, "blob") and res.blob is not None:
            mime = getattr(res, "mimeType", None) or "application/octet-stream"
            return await self._handle_binary(
                base64.b64decode(res.blob), mime, tool_name, user_id, chat_id
            )
        return "[empty embedded resource]"

    def _handle_resource_link(self, link: ResourceLink) -> str:
        """
        ResourceLink carries a URI reference — there is no inline data to store.
        Return a compact, human-readable reference for the LLM context window.
        """
        mime_hint = f" ({link.mimeType})" if link.mimeType else ""
        desc = f" — {link.description}" if link.description else ""
        return f"[resource-link] {link.name}{mime_hint}{desc}: {link.uri}"

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _store_and_summarize(
        self,
        data: bytes,
        mime_type: str,
        tool_name: str,
        user_id: str,
        chat_id: str,
    ) -> str:
        result = await self.artifact_store.save(
            data=data,
            mime_type=mime_type,
            user_id=user_id,
            chat_id=chat_id,
            tool_name=tool_name,
        )
        await self.artifact_repo.save(
            artifact_id=result["artifact_id"],
            object_key=result["object_key"],
            mime_type=result["mime_type"],
            filename_hint=result["filename_hint"],
            user_id=user_id,
            chat_id=chat_id,
            tool_name=tool_name,
            size_bytes=result["size_bytes"],
        )
        return (
            f"[artifact:{result['artifact_id']}] "
            f"Stored {mime_type} ({result['size_bytes']} bytes). "
            f"Link: {result['link']}"
        )
