#!/bin/zsh
set -e

PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"

PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "未找到 Python 3.11+。请先安装 Python 3.11 或 3.12。"
  read -k 1
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "当前 Python 版本过低；本项目需要 Python 3.11+。"
  read -k 1
  exit 1
}

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "正在创建 macOS 虚拟环境..."
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
fi

echo "正在检查项目依赖..."
"$PROJECT_DIR/.venv/bin/python" -m pip install -e "${PROJECT_DIR}[audio,pdf]"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/tools/macos_launcher.py"
