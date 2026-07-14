#!/usr/bin/env bash
# Crypto Analyst Web：每次执行 = 先停掉占用端口的旧进程，再启动（等价于重启）。
#
# 用法：
#   ./scripts/run-web.sh
#   WEB_PORT=9000 ./scripts/run-web.sh
#   WEB_HOST=0.0.0.0 ./scripts/run-web.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8000}"

pids="$(lsof -ti ":${WEB_PORT}" 2>/dev/null || true)"
if [[ -n "${pids}" ]]; then
  echo "释放端口 ${WEB_PORT}，结束进程: ${pids}"
  # shellcheck disable=SC2086
  kill -9 ${pids} 2>/dev/null || true
  sleep 0.4
fi

echo "启动 Web: http://${WEB_HOST}:${WEB_PORT}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m analyst.cli web --no-open --host "$WEB_HOST" --port "$WEB_PORT"
fi

if command -v uv >/dev/null 2>&1; then
  exec uv run python -m analyst.cli web --no-open --host "$WEB_HOST" --port "$WEB_PORT"
fi

echo "未找到 .venv/bin/python，且未安装 uv。请在项目根目录执行: uv sync 或 python -m venv .venv && pip install -e '.[web]'" >&2
exit 1
