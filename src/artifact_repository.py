import asyncpg


class ArtifactRepository:

    def __init__(self, db_pool: asyncpg.Pool):
        self.pool = db_pool

    async def save(
        self,
        artifact_id: str,
        object_key: str,
        mime_type: str,
        filename_hint: str,
        user_id: str,
        chat_id: str,
        tool_name: str,
        size_bytes: int,
    ):
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO artifacts
                    (artifact_id, object_key, mime_type, filename_hint,
                     user_id, chat_id, tool_name, size_bytes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                artifact_id, object_key, mime_type, filename_hint,
                user_id, chat_id, tool_name, size_bytes,
            )

    async def get(self, artifact_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM artifacts WHERE artifact_id = $1",
                artifact_id,
            )
            return dict(row) if row else None

    async def list_by_chat(self, chat_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM artifacts WHERE chat_id = $1 ORDER BY created_at DESC",
                chat_id,
            )
            return [dict(r) for r in rows]

    async def list_by_user(self, user_id: str) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM artifacts WHERE user_id = $1 ORDER BY created_at DESC",
                user_id,
            )
            return [dict(r) for r in rows]

    async def delete(self, artifact_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = $1",
                artifact_id,
            )
