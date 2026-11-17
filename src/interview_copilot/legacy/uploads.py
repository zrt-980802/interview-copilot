"""Bounded temporary storage for the legacy multipart endpoints."""

from contextlib import asynccontextmanager
from pathlib import Path
import re
from tempfile import TemporaryDirectory


class UploadTooLarge(ValueError):
    pass


@asynccontextmanager
async def saved_upload(upload, *, max_bytes: int = 25 * 1024 * 1024):
    name = (upload.filename or "upload.bin").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "upload.bin"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if name.split(".")[0].upper() in reserved:
        name = "_" + name
    if len(name) > 180:
        name = name[:140] + Path(name).suffix[:30]
    try:
        with TemporaryDirectory(prefix="interview-upload-") as directory:
            path = Path(directory) / name
            total = 0
            with path.open("xb") as target:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UploadTooLarge("文件超过上传大小限制")
                    target.write(chunk)
            yield str(path)
    finally:
        await upload.close()
