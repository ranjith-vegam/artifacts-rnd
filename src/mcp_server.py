"""
MCP server that explicitly constructs CallToolResult for every tool,
covering all five content types defined in the MCP 2025-06-18 specification:

  TextContent          type="text"          — text string
  ImageContent         type="image"         — base64 binary + mimeType
  AudioContent         type="audio"         — base64 binary + mimeType
  ResourceLink         type="resource_link" — URI reference (not inline data)
  EmbeddedResource     type="resource"      — inline text or blob resource

Tools
──────────────────────────────────────────────────────────────────────────────
  echo_short                → TextContent  (short — stays inline, not stored)
  get_large_report          → TextContent  (large plain text — stored)
  render_dashboard          → TextContent  (HTML — stored)
  query_records             → TextContent  (JSON — stored)
  export_csv                → TextContent  (CSV — stored)
  get_pixel_image           → ImageContent (PNG binary — stored)
  get_audio_clip            → AudioContent (WAV binary — stored)
  get_resource_link         → ResourceLink (URI reference — not stored)
  get_embedded_text         → EmbeddedResource / TextResourceContents (stored)
  get_embedded_blob         → EmbeddedResource / BlobResourceContents (stored)
  get_mixed_result          → TextContent + ImageContent  (caption inline, image stored)
"""
from __future__ import annotations

import base64
import json
import struct
import zlib

from mcp.server.fastmcp import FastMCP
from mcp.types import (
    AudioContent,
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

mcp = FastMCP("artifact-demo-server")


# ── helpers ────────────────────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _make_png_8x8() -> bytes:
    """Build a valid 8×8 RGB PNG (no external deps)."""
    width = height = 8

    def _chunk(tag: bytes, body: bytes) -> bytes:
        raw = tag + body
        return struct.pack(">I", len(body)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    raw_rows = b""
    for y in range(height):
        raw_rows += b"\x00"
        for x in range(width):
            raw_rows += bytes([int(x * 255 / 7), int(y * 255 / 7), 128])

    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw_rows))
        + _chunk(b"IEND", b"")
    )


def _make_wav_silence(ms: int = 100, rate: int = 8000) -> bytes:
    """Build a minimal PCM WAV file containing silence."""
    samples = int(rate * ms / 1000)
    audio = b"\x00\x00" * samples
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(audio), b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(audio),
    ) + audio


# ── tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def echo_short(message: str) -> CallToolResult:
    """Short confirmation — returned inline, never stored as artifact."""
    return CallToolResult(content=[
        TextContent(type="text", text=f"OK: {message}"),
    ])


@mcp.tool()
def get_large_report(lines: int = 100) -> CallToolResult:
    """Large plain-text system report — stored as text/plain artifact."""
    header = "SYSTEM REPORT\n" + "=" * 60 + "\n"
    body = "\n".join(
        f"[{i:04d}] host=node-{i % 8}  cpu={i % 100:.1f}%  mem={i * 7 % 100:.1f}%  status=OK"
        for i in range(lines)
    )
    return CallToolResult(content=[
        TextContent(type="text", text=header + body),
    ])


@mcp.tool()
def render_dashboard(title: str = "Metrics Dashboard") -> CallToolResult:
    """HTML dashboard page — stored as text/html artifact."""
    rows = "\n".join(
        f"<tr><td>{i}</td><td>service-{i}</td><td>{'UP' if i % 5 else 'DOWN'}</td></tr>"
        for i in range(1, 21)
    )
    html = (
        f"<!DOCTYPE html>\n<html lang='en'>\n"
        f"<head><meta charset='utf-8'><title>{title}</title></head>\n"
        f"<body>\n  <h1>{title}</h1>\n"
        f"  <table border='1'>\n"
        f"    <thead><tr><th>#</th><th>Service</th><th>Status</th></tr></thead>\n"
        f"    <tbody>{rows}</tbody>\n"
        f"  </table>\n</body>\n</html>"
    )
    return CallToolResult(content=[
        TextContent(type="text", text=html),
    ])


@mcp.tool()
def query_records(table: str = "users", limit: int = 60) -> CallToolResult:
    """JSON query results — stored as application/json artifact."""
    records = [
        {"id": i, "name": f"User {i}", "email": f"user{i}@example.com", "active": bool(i % 2)}
        for i in range(limit)
    ]
    return CallToolResult(content=[
        TextContent(type="text", text=json.dumps({"table": table, "count": limit, "records": records}, indent=2)),
    ])


@mcp.tool()
def export_csv(rows: int = 60) -> CallToolResult:
    """CSV data — stored as text/csv artifact."""
    lines = ["id,name,email,score,active"]
    for i in range(rows):
        lines.append(f"{i},User {i},user{i}@example.com,{i * 1.5:.1f},{i % 2 == 0}")
    return CallToolResult(content=[
        TextContent(type="text", text="\n".join(lines)),
    ])


@mcp.tool()
def get_pixel_image() -> CallToolResult:
    """8×8 RGB PNG — stored as image/png artifact."""
    return CallToolResult(content=[
        ImageContent(type="image", data=_b64(_make_png_8x8()), mimeType="image/png"),
    ])


@mcp.tool()
def get_audio_clip() -> CallToolResult:
    """100ms WAV silence — stored as audio/wav artifact."""
    return CallToolResult(content=[
        AudioContent(type="audio", data=_b64(_make_wav_silence()), mimeType="audio/wav"),
    ])


@mcp.tool()
def get_resource_link(name: str = "latest-report") -> CallToolResult:
    """
    URI reference to an external resource — returned inline as a reference,
    not stored as a MinIO artifact (no inline data to store).
    """
    return CallToolResult(content=[
        ResourceLink(
            type="resource_link",
            uri=f"https://reports.internal/data/{name}.json",  # type: ignore[arg-type]
            name=name,
            description=f"External report: {name}",
            mimeType="application/json",
        ),
    ])


@mcp.tool()
def get_embedded_text(label: str = "config") -> CallToolResult:
    """
    Inline text resource via EmbeddedResource / TextResourceContents.
    Content will be detected as JSON and stored as application/json artifact.
    """
    payload = json.dumps(
        {"label": label, "items": [{"k": f"key-{i}", "v": i * 10} for i in range(60)]},
        indent=2,
    )
    return CallToolResult(content=[
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=f"resource:///configs/{label}.json",  # type: ignore[arg-type]
                mimeType="application/json",
                text=payload,
            ),
        ),
    ])


@mcp.tool()
def get_embedded_blob(label: str = "thumbnail") -> CallToolResult:
    """
    Inline binary resource via EmbeddedResource / BlobResourceContents.
    PNG bytes are base64-encoded per spec and stored as image/png artifact.
    """
    return CallToolResult(content=[
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"resource:///images/{label}.png",  # type: ignore[arg-type]
                mimeType="image/png",
                blob=_b64(_make_png_8x8()),
            ),
        ),
    ])


@mcp.tool()
def get_mixed_result(label: str = "chart") -> CallToolResult:
    """
    Two content items: a short TextContent (inline) + an ImageContent (stored).
    Demonstrates that a single CallToolResult can carry heterogeneous content.
    """
    caption = f"Chart label: {label}\nGenerated at: 2026-03-18\nData points: 8"
    return CallToolResult(content=[
        TextContent(type="text", text=caption),
        ImageContent(type="image", data=_b64(_make_png_8x8()), mimeType="image/png"),
    ])


if __name__ == "__main__":
    mcp.run(transport="stdio")
