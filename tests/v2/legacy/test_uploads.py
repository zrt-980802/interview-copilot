import asyncio
from pathlib import Path

import pytest

from interview_copilot.legacy.uploads import saved_upload, UploadTooLarge


class Upload:
    def __init__(self, filename, content):
        self.filename = filename
        self.content = content
        self.closed = False

    async def read(self, size):
        chunk, self.content = self.content[:size], self.content[size:]
        return chunk

    async def close(self):
        self.closed = True


def test_concurrent_uploads_keep_names_but_use_distinct_paths_and_cleanup():
    async def scenario():
        first, second = Upload("../notes.md", b"first"), Upload("notes.md", b"second")
        async with saved_upload(first) as a:
            async with saved_upload(second) as b:
                assert a != b
                assert Path(a).name == Path(b).name == "notes.md"
                assert Path(a).read_bytes() == b"first"
                assert Path(b).read_bytes() == b"second"
        assert not Path(a).exists() and not Path(b).exists()
        assert first.closed and second.closed
    asyncio.run(scenario())


def test_oversized_and_failed_uploads_cleanup():
    async def scenario():
        upload = Upload("notes.md", b"too long")
        with pytest.raises(UploadTooLarge):
            async with saved_upload(upload, max_bytes=3):
                pytest.fail("oversized file reached ingestion")
        assert upload.closed
        upload = Upload("notes.md", b"content")
        with pytest.raises(RuntimeError):
            async with saved_upload(upload) as path:
                raise RuntimeError("ingestion failed")
        assert not Path(path).exists()
        assert upload.closed
    asyncio.run(scenario())
