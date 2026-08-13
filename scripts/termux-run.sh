#!/usr/bin/env bash
# 在手机 Termux 里启动盯盘后端（本机 127.0.0.1:8000）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-8000}"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "未找到 .venv。请先运行: bash scripts/termux-setup.sh" >&2
  exit 1
fi

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -ti ":${WEB_PORT}" 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "释放端口 ${WEB_PORT}: ${pids}"
    # shellcheck disable=SC2086
    kill -9 ${pids} 2>/dev/null || true
    sleep 0.3
  fi
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

echo "手机本机服务: http://${WEB_HOST}:${WEB_PORT}"
echo "浏览器打开上述地址，或打开「盯盘」APK → 本机 Termux"
echo "保持 Termux 会话不要关；电源里关掉 Termux 电池优化。"

exec "$ROOT/.venv/bin/python" -m analyst.cli web --no-open --host "$WEB_HOST" --port "$WEB_PORT"
