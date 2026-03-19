"""
Live demo: calls 5 MCP tools, stores results as artifacts, queries the API.

Run with:
    python3 demo.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import httpx
import asyncpg

from mcp import StdioServerParameters
from artifact_store import ArtifactStore
from artifact_repository import ArtifactRepository
from mcp_tool_host import MCPToolHost

# ── config ────────────────────────────────────────────────────────────────

MINIO_URL    = "http://localhost:9002"
MINIO_KEY    = "your-access-key"
MINIO_SECRET = "your-secret-key"
MINIO_BUCKET = "artifacts"
DB_URL       = "postgresql://artifacts_user:artifacts_pass@localhost:5432/artifacts_db"
API_URL      = "http://localhost:8080"

USER_ID = "demo-user"
CHAT_ID = "demo-chat-001"


# ── helpers ────────────────────────────────────────────────────────────────

def banner(title: str):
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")

def step(n: int, text: str):
    print(f"\n[Step {n}] {text}")
    print("─" * 50)


# ── main demo ─────────────────────────────────────────────────────────────

async def main():
    store = ArtifactStore(MINIO_URL, MINIO_KEY, MINIO_SECRET, MINIO_BUCKET)
    await store.ensure_bucket()

    pool = await asyncpg.create_pool(DB_URL)
    repo = ArtifactRepository(pool)

    params = StdioServerParameters(command=sys.executable, args=["src/mcp_server.py"])

    async with MCPToolHost(store, repo, params) as host:

        banner("ARTIFACT STORE — LIVE DEMO")
        print(f"  User  : {USER_ID}")
        print(f"  Chat  : {CHAT_ID}")
        print(f"  MinIO : {MINIO_URL}")
        print(f"  API   : {API_URL}")

        # ── Step 1: Short text — stays inline ─────────────────────────────
        step(1, "Short text → stays inline (NOT stored)")
        result = await host.call_tool(
            "echo_short", {"message": "hello manager"}, USER_ID, CHAT_ID
        )
        print(f"  Tool returned : {result!r}")
        print(f"  ✓ No artifact created — short responses stay in the chat window")

        # ── Step 2: HTML dashboard ─────────────────────────────────────────
        step(2, "HTML dashboard → stored as text/html artifact")
        result = await host.call_tool(
            "render_dashboard", {"title": "Q1 Sales Dashboard"}, USER_ID, CHAT_ID
        )
        print(f"  AI sees  : {result[:120]}...")
        print(f"  ✓ Full HTML page is in MinIO — AI only sees the short summary")

        # ── Step 3: JSON data ──────────────────────────────────────────────
        step(3, "JSON query result → stored as application/json artifact")
        result = await host.call_tool(
            "query_records", {"table": "orders", "limit": 60}, USER_ID, CHAT_ID
        )
        print(f"  AI sees  : {result[:120]}...")
        print(f"  ✓ 60 records stored in MinIO, not in the AI's context window")

        # ── Step 4: PNG image ──────────────────────────────────────────────
        step(4, "PNG image (ImageContent) → stored as image/png artifact")
        result = await host.call_tool("get_pixel_image", {}, USER_ID, CHAT_ID)
        print(f"  AI sees  : {result[:120]}...")
        print(f"  ✓ Binary PNG decoded from base64 and stored directly in MinIO")

        # ── Step 5: WAV audio ──────────────────────────────────────────────
        step(5, "WAV audio (AudioContent) → stored as audio/wav artifact")
        result = await host.call_tool("get_audio_clip", {}, USER_ID, CHAT_ID)
        print(f"  AI sees  : {result[:120]}...")
        print(f"  ✓ Binary WAV decoded from base64 and stored in MinIO")

        # ── Step 6: ResourceLink — never stored ───────────────────────────
        step(6, "ResourceLink → URI reference, no artifact created")
        result = await host.call_tool(
            "get_resource_link", {"name": "q1-forecast"}, USER_ID, CHAT_ID
        )
        print(f"  AI sees  : {result}")
        print(f"  ✓ ResourceLink carries no data — returned inline as a reference")

        # ── Step 7: Mixed content (text + image) ───────────────────────────
        step(7, "Mixed result (TextContent + ImageContent) in one tool call")
        result = await host.call_tool(
            "get_mixed_result", {"label": "Revenue Chart"}, USER_ID, CHAT_ID
        )
        print(f"  AI sees  :\n    {result.replace(chr(10), chr(10) + '    ')}")
        print(f"  ✓ Caption stayed inline, image was stored")

    # ── Step 8: Query the REST API ─────────────────────────────────────────
    step(8, "Query the REST API — what did this chat produce?")
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_URL}/chats/{CHAT_ID}/artifacts")
        artifacts = r.json()

    print(f"  Total artifacts stored this chat: {len(artifacts)}")
    print()
    for a in artifacts:
        print(f"  • [{a['mime_type']:25s}]  {a['artifact_id'][:12]}...  tool={a['tool_name']}")

    # ── Step 9: Refresh a link ─────────────────────────────────────────────
    if artifacts:
        step(9, f"Refresh an expired link for artifact: {artifacts[0]['artifact_id'][:12]}...")
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{API_URL}/artifacts/{artifacts[0]['artifact_id']}/link")
        data = r.json()
        print(f"  Fresh link : {data['link'][:80]}...")
        print(f"  MIME type  : {data['mime_type']}")
        print(f"  Filename   : {data['filename_hint']}")
        print(f"  ✓ New presigned URL generated — paste into browser to open")

    banner("DEMO COMPLETE")
    print()
    print("  Open these in your browser:")
    print(f"    Swagger UI     → {API_URL}/docs")
    print(f"    MinIO Console  → http://localhost:9003")
    print(f"      login: your-access-key / your-secret-key")
    print(f"      bucket: {MINIO_BUCKET}  →  {USER_ID}/{CHAT_ID}/")
    print()
    print("  Get a browser-ready link for any artifact:")
    print(f"    curl -s {API_URL}/artifacts/<artifact_id>/link | python3 -m json.tool")
    print()

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
