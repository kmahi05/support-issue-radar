"""Local file storage for uploads (CSV imports), laid out as STORAGE_DIR/{org_id}/..."""

# shortcut: local disk storage, single API server; move to S3-compatible storage before running 2+ instances # noqa: E501

from pathlib import Path
from uuid import UUID

import anyio
from fastapi import UploadFile

from .config import get_settings
from .errors import ApiError

CHUNK = 1024 * 1024
CSV_TYPES = {"text/csv", "application/vnd.ms-excel", "text/plain", "application/octet-stream"}


def org_path(org_id: UUID, *parts: str) -> Path:
    root = get_settings().STORAGE_DIR.resolve()
    path = root.joinpath(str(org_id), *parts).resolve()
    if not path.is_relative_to(root / str(org_id)):
        raise ApiError(400, "bad_path", "Invalid storage path")
    return path


async def save_csv_upload(upload: UploadFile, org_id: UUID, name: str, max_bytes: int) -> Path:
    """Stream an uploaded CSV to disk in 1 MB chunks, enforcing the size cap while streaming."""
    if not (upload.filename or "").lower().endswith(".csv") or (
        upload.content_type and upload.content_type not in CSV_TYPES
    ):
        raise ApiError(415, "unsupported_file", "Only .csv files are accepted")
    dest = org_path(org_id, "imports", name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        async with await anyio.open_file(dest, "wb") as out:
            while chunk := await upload.read(CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError(413, "payload_too_large", f"File exceeds {max_bytes} bytes")
                await out.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest
