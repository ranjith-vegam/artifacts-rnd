"""
Shared fixtures: spins up real MinIO and Postgres containers once per test session.
All async fixtures use the same event loop (asyncio_default_fixture_loop_scope = "session").
"""
import pathlib
import sys

import asyncpg
import pytest
from mcp.client.stdio import StdioServerParameters
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from artifact_repository import ArtifactRepository
from artifact_store import ArtifactStore
from mcp_tool_host import MCPToolHost

MINIO_ACCESS_KEY = "testaccesskey"
MINIO_SECRET_KEY = "testsecretkey"   # must be ≥ 8 chars
MINIO_BUCKET = "test-artifacts"
SCHEMA_FILE      = pathlib.Path(__file__).parent.parent / "schema.sql"
MCP_SERVER_SCRIPT = pathlib.Path(__file__).parent.parent / "src" / "mcp_server.py"


# ---------------------------------------------------------------------------
# Container fixtures (synchronous — Docker operations are blocking)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def minio_container():
    container = (
        DockerContainer("minio/minio:latest")
        .with_command("server /data --console-address :9001")
        .with_env("MINIO_ROOT_USER", MINIO_ACCESS_KEY)
        .with_env("MINIO_ROOT_PASSWORD", MINIO_SECRET_KEY)
        .with_exposed_ports(9000)
    )
    with container:
        wait_for_logs(container, "API", timeout=60)
        yield container


@pytest.fixture(scope="session")
def pg_container():
    with PostgresContainer("postgres:16") as container:
        yield container


# ---------------------------------------------------------------------------
# Derived URL fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def minio_endpoint(minio_container):
    host = minio_container.get_container_host_ip()
    port = minio_container.get_exposed_port(9000)
    return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def pg_dsn(pg_container):
    return pg_container.get_connection_url().replace("postgresql+psycopg2", "postgresql")


# ---------------------------------------------------------------------------
# Async infrastructure fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
async def db_pool(pg_dsn):
    pool = await asyncpg.create_pool(pg_dsn)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_FILE.read_text())
    yield pool
    await pool.close()


@pytest.fixture(scope="session")
async def store(minio_endpoint):
    s = ArtifactStore(
        endpoint_url=minio_endpoint,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        bucket_name=MINIO_BUCKET,
        presign_expiry_seconds=3600,
    )
    await s.ensure_bucket()
    return s


@pytest.fixture(scope="session")
async def repo(db_pool):
    return ArtifactRepository(db_pool)


@pytest.fixture(scope="session")
async def tool_host(store, repo):
    """
    MCPToolHost connected to the real mcp_server.py via stdio for the entire
    test session.  The server process is started once and reused across tests.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER_SCRIPT)],
    )
    host = MCPToolHost(store, repo, params)
    await host.connect()
    yield host
    try:
        await host.close()
    except (RuntimeError, BaseException):
        pass
