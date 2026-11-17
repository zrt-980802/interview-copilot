"""Import a specific public GitHub Markdown file into the local document library."""

from pathlib import PurePosixPath
import asyncio
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from interview_copilot.services.documents import DocumentError


def github_markdown_url(url):
    if not isinstance(url,str) or len(url) > 2048:
        raise DocumentError("请输入具体的公开 GitHub Markdown 文件链接")
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError()
        parts = parsed.path.split("/")
        if parsed.hostname == "github.com" and len(parts) >= 6 and parts[3] == "blob":
            raw_path = "/".join(parts[:3] + parts[4:])
        elif parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 5:
            raw_path = parsed.path
        else:
            raise ValueError()
        name = PurePosixPath(unquote(raw_path)).name
        if (PurePosixPath(name).suffix.lower() not in {".md",".markdown"}
                or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)
                or len(name) > 180 or any(part in {".","..",""} for part in raw_path.split("/")[1:])):
            raise ValueError()
    except ValueError as exc:
        raise DocumentError("仅支持 github.com 的具体公开 Markdown 文件链接，或对应的 raw 链接") from exc
    return urlunsplit(("https","raw.githubusercontent.com",raw_path,"","")), name


async def download_github(url, directory, *, transport=None):
    canonical, name = github_markdown_url(url)
    path = directory / name
    try:
        async with asyncio.timeout(30), httpx.AsyncClient(timeout=20,follow_redirects=False,transport=transport) as client:
            async with client.stream("GET",canonical) as response:
                if response.status_code != 200:
                    raise DocumentError("无法读取公开 GitHub 文件，请检查链接；当前不支持私有仓库")
                size = 0
                with path.open("xb") as target:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 5 * 1024 * 1024:
                            raise DocumentError("GitHub Markdown 超过 5 MB，请拆分后导入")
                        target.write(chunk)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise DocumentError("GitHub 连接失败或超时，请稍后重试") from exc
    return path, name, canonical
