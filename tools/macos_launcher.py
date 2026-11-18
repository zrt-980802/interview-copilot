"""Start the local interview copilot from a macOS double-click launcher."""

from __future__ import annotations

import json
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent


def endpoint_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def ensure_config() -> dict:
    target = PROJECT_DIR / "config.json"
    if not target.is_file():
        shutil.copyfile(PROJECT_DIR / "config.example.json", target)
        print(f"已创建本地配置: {target}")
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    if sys.platform != "darwin":
        print("这个启动器仅用于 macOS。", file=sys.stderr)
        return 2

    config = ensure_config()
    port = int(config.get("port", 8527))
    base_url = f"http://127.0.0.1:{port}"
    stt_url = str(config.get("stt", {}).get("url", "")).rstrip("/")
    if stt_url and not endpoint_ready(f"{stt_url}/models", timeout=2.0):
        print(f"提示: STT 服务暂不可用 ({stt_url})，采集可以启动，但语音无法转写。")

    if endpoint_ready(f"{base_url}/api/health"):
        print(f"服务已在运行: {base_url}")
        webbrowser.open(base_url)
        return 0

    process = subprocess.Popen(
        [sys.executable, str(PROJECT_DIR / "server.py"), "--port", str(port)],
        cwd=PROJECT_DIR,
    )
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    for _ in range(80):
        if process.poll() is not None:
            print(f"服务启动失败，退出码 {process.returncode}。", file=sys.stderr)
            return process.returncode or 1
        if endpoint_ready(f"{base_url}/api/health"):
            print(f"面试助手已启动: {base_url}")
            webbrowser.open(base_url)
            break
        time.sleep(0.25)
    else:
        stop()
        print("服务在 20 秒内没有就绪。", file=sys.stderr)
        return 1

    try:
        code = process.wait()
        return 0 if stopping and code < 0 else code
    except KeyboardInterrupt:
        stop()
        try:
            code = process.wait(timeout=8)
            return 0 if code < 0 else code
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
            return 0
    finally:
        if stopping and process.poll() is None:
            process.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
