import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import asyncpg
from fastapi import FastAPI, HTTPException

from artifact_repository import ArtifactRepository
from artifact_store import ArtifactStore

artifact_store: ArtifactStore = None
artifact_repo: ArtifactRepository = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global artifact_store, artifact_repo

    artifact_store = ArtifactStore(
        endpoint_url=os.environ["MINIO_ENDPOINT_URL"],
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        bucket_name=os.environ.get("MINIO_BUCKET_NAME", "artifacts"),
        presign_expiry_seconds=int(os.environ.get("PRESIGN_EXPIRY_SECONDS", "3600")),
        presign_endpoint_url=os.environ.get("PRESIGN_ENDPOINT_URL"),
    )
    await artifact_store.ensure_bucket()

    db_pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    artifact_repo = ArtifactRepository(db_pool)

    yield

    await db_pool.close()


app = FastAPI(title="Artifact Store", lifespan=lifespan)


@app.get("/artifacts/{artifact_id}/link")
async def get_artifact_link(artifact_id: str):
    record = await artifact_repo.get(artifact_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    fresh_link = await artifact_store.get_fresh_link(record["object_key"])
    return {
        "artifact_id": artifact_id,
        "link": fresh_link,
        "mime_type": record["mime_type"],
        "filename_hint": record["filename_hint"],
    }


@app.get("/artifacts/{artifact_id}")
async def get_artifact_metadata(artifact_id: str):
    record = await artifact_repo.get(artifact_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return record


@app.get("/chats/{chat_id}/artifacts")
async def list_chat_artifacts(chat_id: str):
    return await artifact_repo.list_by_chat(chat_id)


@app.get("/users/{user_id}/artifacts")
async def list_user_artifacts(user_id: str):
    return await artifact_repo.list_by_user(user_id)


@app.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    record = await artifact_repo.get(artifact_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    await artifact_store.delete(record["object_key"])
    await artifact_repo.delete(artifact_id)
    return {"deleted": artifact_id}
