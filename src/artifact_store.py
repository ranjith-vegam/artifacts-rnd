import uuid
import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError


class ArtifactStore:

    def __init__(
        self,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket_name: str,
        presign_expiry_seconds: int = 3600,
        max_size_bytes: int = 50 * 1024 * 1024,  # 50MB
        presign_endpoint_url: str = None,
    ):
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name
        self.presign_expiry_seconds = presign_expiry_seconds
        self.max_size_bytes = max_size_bytes
        # URL baked into presigned links — must be reachable by the browser/client.
        # Defaults to endpoint_url when not set (e.g. dev, tests).
        self.presign_endpoint_url = presign_endpoint_url or endpoint_url

        self.session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def _client(self):
        """Client for actual S3 operations (put, head, delete). Uses internal URL."""
        return self.session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            config=Config(signature_version="s3v4"),
        )

    def _presign_client(self):
        """Client used only for generating presigned URLs. Uses the public URL."""
        return self.session.client(
            "s3",
            endpoint_url=self.presign_endpoint_url,
            config=Config(signature_version="s3v4"),
        )

    async def ensure_bucket(self):
        """Create the bucket if it does not exist."""
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self.bucket_name)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
                    await s3.create_bucket(Bucket=self.bucket_name)
                else:
                    raise

    async def save(
        self,
        data: bytes,
        mime_type: str,
        user_id: str,
        chat_id: str,
        tool_name: str,
        filename_hint: str = None,
    ) -> dict:
        if len(data) > self.max_size_bytes:
            raise ValueError(
                f"Artifact size {len(data)} bytes exceeds limit of {self.max_size_bytes} bytes"
            )

        artifact_id = uuid.uuid4().hex
        ext = self._ext_from_mime(mime_type)
        object_key = f"{user_id}/{chat_id}/{artifact_id}{ext}"
        hint = filename_hint or f"{tool_name}-result{ext}"

        async with self._client() as s3:
            await s3.put_object(
                Bucket=self.bucket_name,
                Key=object_key,
                Body=data,
                ContentType=mime_type,
                Metadata={
                    "artifact-id": artifact_id,
                    "filename-hint": hint,
                    "user-id": user_id,
                    "chat-id": chat_id,
                },
            )

        async with self._presign_client() as s3:
            presigned_url = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": object_key},
                ExpiresIn=self.presign_expiry_seconds,
            )

        return {
            "artifact_id": artifact_id,
            "object_key": object_key,
            "link": presigned_url,
            "mime_type": mime_type,
            "filename_hint": hint,
            "size_bytes": len(data),
            "expires_in_seconds": self.presign_expiry_seconds,
        }

    async def get_fresh_link(self, object_key: str, download: bool = False) -> str:
        """
        Returns a presigned URL for the given object.

        download=False (default): browser renders inline (image shown, HTML displayed, etc.)
        download=True: browser always prompts a file download regardless of MIME type.
        """
        async with self._client() as s3:
            try:
                meta = await s3.head_object(Bucket=self.bucket_name, Key=object_key)
            except ClientError:
                raise FileNotFoundError(f"Artifact not found: {object_key}")

        params = {"Bucket": self.bucket_name, "Key": object_key}
        if download:
            filename = meta.get("Metadata", {}).get("filename-hint") or object_key.split("/")[-1]
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'

        async with self._presign_client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=self.presign_expiry_seconds,
            )

    async def delete(self, object_key: str):
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket_name, Key=object_key)

    def _ext_from_mime(self, mime_type: str) -> str:
        # Strip parameters (e.g. "text/html; charset=utf-8" → "text/html")
        base = mime_type.split(";")[0].strip().lower()
        mapping = {
            # Text
            "text/plain": ".txt",
            "text/html": ".html",
            "text/csv": ".csv",
            "text/markdown": ".md",
            "text/xml": ".xml",
            # Application
            "application/json": ".json",
            "application/xml": ".xml",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "application/octet-stream": ".bin",
            # Images
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            # Audio
            "audio/wav": ".wav",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/ogg": ".ogg",
            "audio/webm": ".webm",
            "audio/aac": ".aac",
            # Video
            "video/mp4": ".mp4",
            "video/webm": ".webm",
        }
        return mapping.get(base, ".bin")
