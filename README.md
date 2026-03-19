# Artifact Store

An on-premises artifact storage system that sits between your AI assistant (Claude, GPT, etc.) and your infrastructure. When an MCP tool produces large or binary output — HTML reports, images, audio clips, CSV exports, JSON datasets — the system automatically saves it to **MinIO** (S3-compatible object storage), records metadata in **Postgres**, and hands back a presigned URL that opens directly in the browser.

---

## Problem Statement

MCP tools return content inside `CallToolResult`. That content can be:

| Content type | What it is | Example |
|---|---|---|
| `TextContent` | Plain text, HTML, JSON, CSV, Markdown | A generated dashboard, a query result |
| `ImageContent` | Base64-encoded image | A chart, a pixel map |
| `AudioContent` | Base64-encoded audio | A voice clip, a TTS output |
| `ResourceLink` | A URI reference (external resource) | A link to an existing document |
| `EmbeddedResource` | Inline text or binary blob | A file embedded inside the tool response |

Most of these are too large or wrong-type to include inline in the chat. This system:

1. Receives the `CallToolResult` from the MCP server
2. Inspects each content item and decides whether to store it as an artifact
3. Uploads storable items to MinIO and records them in Postgres
4. Returns a presigned URL (browser-accessible) or inline text (for short snippets)

---

## Architecture

```
Claude / AI Assistant
        │
        │  tool call (name, arguments, user_id, chat_id)
        ▼
┌─────────────────────┐
│   MCPToolHost       │  ◄── MCP client (stdio)
│   (mcp_tool_host.py)│
└────────┬────────────┘
         │  stdio (JSON-RPC)
         ▼
┌─────────────────────┐
│   MCP Server        │  FastMCP — returns explicit CallToolResult
│   (mcp_server.py)   │
└────────┬────────────┘
         │  CallToolResult (TextContent / ImageContent / AudioContent /
         │                  ResourceLink / EmbeddedResource)
         ▼
┌─────────────────────┐     ┌──────────────────┐
│   ArtifactStore     │────►│   MinIO          │  object storage
│   (artifact_store)  │     │   (S3-compatible) │
└────────┬────────────┘     └──────────────────┘
         │
         ▼
┌─────────────────────┐     ┌──────────────────┐
│ ArtifactRepository  │────►│   Postgres       │  metadata + index
│ (artifact_repo)     │     │   (artifacts tbl) │
└─────────────────────┘     └──────────────────┘
         │
         ▼
┌─────────────────────┐
│   FastAPI (main.py) │  REST API — browse, fetch, delete artifacts
└─────────────────────┘
```

---

## Project Structure

```
artifact-store/
├── src/                        # All application source code
│   ├── artifact_store.py       # MinIO upload, presigned URL generation
│   ├── artifact_repository.py  # Postgres CRUD for artifact metadata
│   ├── mcp_tool_host.py        # MCP client + content routing + storage decisions
│   └── mcp_server.py           # FastMCP server with 11 demo tools
├── tests/
│   ├── conftest.py             # Session-scoped MinIO + Postgres containers
│   ├── test_artifact_store.py  # ArtifactStore unit/integration tests
│   ├── test_artifact_repository.py  # ArtifactRepository integration tests
│   ├── test_mcp_tool_host.py   # End-to-end MCP client → server → storage tests
│   └── test_api.py             # FastAPI endpoint integration tests
├── main.py                     # FastAPI app — REST endpoints
├── schema.sql                  # Postgres DDL
├── docker-compose.yml          # MinIO + Postgres + app services
├── Dockerfile                  # Production container
├── pyproject.toml              # Dependencies + pytest config
└── .env.example                # Environment variable template
```

---

## Component Explanations

### `src/artifact_store.py` — MinIO Interface

**Responsibility**: Upload bytes to MinIO and generate browser-accessible presigned URLs.

**Key design decisions**:

- Uses two separate S3 client instances:
  - `_client()` — connects via `endpoint_url` (internal Docker hostname like `http://minio:9000`). Used for all actual S3 operations (upload, delete, head).
  - `_presign_client()` — connects via `presign_endpoint_url` (public URL like `http://192.168.1.49:9002`). Used only to generate presigned URLs.

  This is critical because AWS Signature V4 includes `host` as a signed header. If you generate a URL using `http://minio:9000` as the endpoint, the URL contains `Host: minio:9000` baked into the signature. When a browser opens that URL from outside Docker, it sends `Host: 192.168.1.49:9002`, which causes `SignatureDoesNotMatch`. Generating the URL with the public hostname from the start solves this.

- Object key path: `{user_id}/{chat_id}/{artifact_id}.{ext}` — allows listing all artifacts for a user or chat efficiently.

- MIME-to-extension mapping covers HTML, JSON, CSV, plain text, PNG, JPEG, GIF, WebP, SVG, WAV, MP3, OGG, Markdown, XML, PDF, and falls back to `.bin`.

**Core methods**:
```python
await store.save(data, mime_type, user_id, chat_id, tool_name, filename_hint=None)
# → {"artifact_id": "...", "object_key": "...", "link": "https://...", "mime_type": "...", ...}

await store.get_fresh_link(object_key)
# → "https://..." — new presigned URL for an existing object

await store.delete(object_key)
# → None — removes from MinIO (idempotent, never raises for missing keys)
```

---

### `src/artifact_repository.py` — Postgres Metadata Store

**Responsibility**: Record artifact metadata (IDs, MIME type, owner, timestamps) so artifacts can be listed, filtered, and deleted without hitting MinIO.

**Table**: `artifacts` — see `schema.sql` for DDL.

**Core methods**:
```python
await repo.save(artifact_id, object_key, mime_type, filename_hint,
                user_id, chat_id, tool_name, size_bytes)

await repo.get(artifact_id)           # → dict or None
await repo.list_by_chat(chat_id)      # → [dict, ...] newest first
await repo.list_by_user(user_id)      # → [dict, ...] across all chats
await repo.delete(artifact_id)        # → None (no-op if missing)
```

---

### `src/mcp_tool_host.py` — Orchestrator

**Responsibility**: Act as an MCP client, call tools on the MCP server, receive `CallToolResult`, and decide what to store vs. return inline.

**Storage decision logic** (`should_store_text`):
- Always store: `text/html`, `application/json`, `text/csv`, `text/markdown`, `text/xml`, `application/xml`, `application/pdf`
- Store if large (> 500 chars): `text/plain`, `text/x-python`, `text/javascript`, `text/css`, unknown text types
- Never store: short plain text (it's just a direct reply)

**MIME detection for `TextContent`** (`detect_text_mime`):
`TextContent` has no `mimeType` field — the MIME must be sniffed from the content itself. Detection order:
1. HTML: starts with `<html`, `<!doctype html`, or `<div`/`<p`/`<h1` + `<`
2. JSON: starts with `{` or `[` and `json.loads()` succeeds
3. CSV: contains `,` on multiple lines with consistent column counts
4. Plain text: fallback

**Content routing** (`_dispatch`):

| Content type | Action |
|---|---|
| `TextContent` | Detect MIME → store if `should_store_text` → return URL or inline text |
| `ImageContent` | Decode base64 → store → return URL |
| `AudioContent` | Decode base64 → store → return URL |
| `ResourceLink` | Never store → return formatted reference string |
| `EmbeddedResource` | `.text` → treat as `TextContent`; `.blob` → decode base64 → store |

**Usage**:
```python
host = MCPToolHost(store, repo, StdioServerParameters(...))
await host.connect()

results = await host.call_tool(
    tool_name="render_dashboard",
    arguments={"title": "Q1 Sales"},
    user_id="user-123",
    chat_id="chat-456",
)
# results → list of strings: URLs for stored artifacts, inline text for short content
```

---

### `src/mcp_server.py` — Demo MCP Server

**Responsibility**: A FastMCP server that demonstrates all 5 MCP content types. Every tool explicitly returns `CallToolResult(content=[...])` — this is important because FastMCP detects the return type annotation and passes `CallToolResult` through unchanged without auto-wrapping.

**Tools**:

| Tool | Content type | MIME | Stored? |
|---|---|---|---|
| `echo_short` | `TextContent` | plain text (short) | No |
| `get_large_report` | `TextContent` | plain text (large) | Yes |
| `render_dashboard` | `TextContent` | HTML | Yes |
| `query_records` | `TextContent` | JSON | Yes |
| `export_csv` | `TextContent` | CSV | Yes |
| `get_pixel_image` | `ImageContent` | PNG | Yes |
| `get_audio_clip` | `AudioContent` | WAV | Yes |
| `get_resource_link` | `ResourceLink` | — | No |
| `get_embedded_text` | `EmbeddedResource` (text) | JSON | Yes |
| `get_embedded_blob` | `EmbeddedResource` (blob) | PNG | Yes |
| `get_mixed_result` | `TextContent` + `ImageContent` | text + PNG | Text inline, image stored |

---

### `main.py` — REST API

**Responsibility**: FastAPI app that exposes artifacts over HTTP. Used by front-ends, dashboards, or the demo script to browse and retrieve stored artifacts.

**Endpoints**:

```
GET    /artifacts/{id}/link      Refresh presigned URL for an artifact
GET    /artifacts/{id}           Fetch full metadata for an artifact
GET    /chats/{id}/artifacts     List all artifacts in a chat (newest first)
GET    /users/{id}/artifacts     List all artifacts for a user (across chats)
DELETE /artifacts/{id}           Delete artifact from MinIO + Postgres
```

---

### `schema.sql` — Database Schema

```sql
CREATE TABLE artifacts (
    artifact_id   TEXT PRIMARY KEY,
    object_key    TEXT NOT NULL,           -- MinIO path: user/chat/id.ext
    mime_type     TEXT NOT NULL,
    filename_hint TEXT,                    -- human-readable filename
    user_id       TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    tool_name     TEXT,                    -- which MCP tool created this
    size_bytes    BIGINT NOT NULL,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_artifacts_chat ON artifacts(chat_id);
CREATE INDEX idx_artifacts_user ON artifacts(user_id);
```

---

## How to Run

### Prerequisites

- Docker and Docker Compose
- Python 3.12+ with `uv` (`pip install uv`)

### Local development

```bash
# 1. Install dependencies
uv sync

# 2. Start MinIO + Postgres
docker compose up minio postgres -d

# 3. Apply schema
PGPASSWORD=artifacts_pass psql -h localhost -p 5432 -U artifacts_user -d artifacts_db -f schema.sql

# 4. Start the API server
MINIO_ENDPOINT=http://localhost:9000 \
MINIO_ACCESS_KEY=minioadmin \
MINIO_SECRET_KEY=minioadmin123 \
PRESIGN_ENDPOINT_URL=http://localhost:9000 \
DATABASE_URL=postgresql://artifacts_user:artifacts_pass@localhost:5432/artifacts_db \
uv run uvicorn main:app --reload
```

### Docker Compose (full stack)

```bash
# Copy and edit environment
cp .env.example .env
# Edit .env — set PRESIGN_ENDPOINT_URL to your machine's LAN IP if accessing from other devices

docker compose up --build
```

Service URLs:
- API: `http://localhost:8080`
- MinIO console: `http://localhost:9001` (login: `minioadmin` / `minioadmin123`)

### Run tests

Tests spin up real MinIO and Postgres containers via testcontainers — no mocking.

```bash
uv run pytest tests/ -v
```

---

## Demo Walkthrough

Run the demo script to call every MCP tool and inspect the results:

```bash
docker compose up -d
uv run python demo.py
```

The script:
1. Starts the MCP server as a subprocess
2. Calls all 11 tools via `MCPToolHost.call_tool()`
3. Prints what was stored vs. returned inline
4. Lists all artifacts via the REST API

To view a stored artifact, copy the presigned URL from the output and open it in a browser.

---

## Code Traceback Guide

When tracing a request end to end:

**"Where does a tool call start?"**
→ `mcp_tool_host.py` → `MCPToolHost.call_tool()` (line ~60)

**"How does the system decide what to store?"**
→ `mcp_tool_host.py` → `should_store_text()` (module-level function) and `_dispatch()` method

**"How is MIME detected for TextContent?"**
→ `mcp_tool_host.py` → `detect_text_mime()` (module-level function)

**"Where does the file actually go to MinIO?"**
→ `artifact_store.py` → `ArtifactStore.save()` → `s3.put_object()`

**"Where is the presigned URL generated?"**
→ `artifact_store.py` → `ArtifactStore._presign_client()` + `s3.generate_presigned_url()`

**"Where is metadata written to Postgres?"**
→ `artifact_repository.py` → `ArtifactRepository.save()` → `INSERT INTO artifacts`

**"Why are there two S3 clients in ArtifactStore?"**
→ `_client()` uses internal Docker hostname for actual operations; `_presign_client()` uses the public IP/hostname so generated URLs are browser-accessible. AWS Signature V4 signs the `host` header — URL and signer must agree on the hostname.

**"How does the MCP server avoid auto-wrapping?"**
→ `mcp_server.py` — every tool's return type is annotated as `-> CallToolResult`. FastMCP checks `isinstance(result, CallToolResult)` and returns it as-is.

**"Where are REST endpoints defined?"**
→ `main.py` — five routes using `@app.get`/`@app.delete`

---

## Configuration Reference

| Environment variable | Default | Description |
|---|---|---|
| `MINIO_ENDPOINT` | `http://minio:9000` | Internal MinIO URL (used for S3 operations) |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin123` | MinIO secret key |
| `MINIO_BUCKET` | `artifacts` | Bucket name |
| `PRESIGN_ENDPOINT_URL` | same as `MINIO_ENDPOINT` | **Public** URL baked into presigned URLs — must be reachable from the browser. Set to `http://<your-lan-ip>:9002` when accessing from other machines. |
| `DATABASE_URL` | `postgresql://...@postgres:5432/artifacts_db` | Postgres connection string |
| `PRESIGN_EXPIRY_SECONDS` | `3600` | How long presigned URLs remain valid |
| `MAX_ARTIFACT_SIZE_MB` | `50` | Maximum artifact size in megabytes |
