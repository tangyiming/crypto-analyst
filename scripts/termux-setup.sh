#!/usr/bin/env bash
# 在 Android Termux 里安装本项目，让后端在手机上跑。
#
# 用法（在 Termux 里、仓库根目录）：
#   bash scripts/termux-setup.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v pkg >/dev/null 2>&1; then
  echo "请在 Termux 里运行（F-Droid 安装 Termux，不要用 Play 商店旧版）。" >&2
  exit 1
fi

echo "==> 安装系统包"
pkg update -y
pkg install -y python git clang make pkg-config libffi openssl rust lsof termux-api || \
  pkg install -y python git clang make pkg-config libffi openssl lsof

if ! python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"; then
  echo "需要 Python 3.11+，当前: $(python --version 2>&1)" >&2
  exit 1
fi

echo "==> 创建虚拟环境"
python -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip setuptools wheel hatchling

echo "==> 安装 Python 依赖（Android 上跳过 uvloop）"
if ! pip install -e ".[web]"; then
  echo "带 uvloop 的 uvicorn[standard] 失败，改用纯 uvicorn"
  pip install -e .
  pip install "fastapi>=0.115.0" "uvicorn>=0.30.0"
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> 已复制 .env.example → .env，请编辑填入 API Key（nano .env）"
else
  echo "==> 已有 .env，未覆盖"
fi

mkdir -p "$HOME/.shortcuts"
cat > "$HOME/.shortcuts/盯盘" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$ROOT"
exec bash scripts/termux-run.sh
EOF
chmod +x "$HOME/.shortcuts/盯盘"
echo "==> 已写 Termux:Widget 快捷方式：盯盘"

echo
echo "安装完成。下一步："
echo "  1. nano .env   # 填 LLM / Telegram 等"
echo "  2. bash scripts/termux-run.sh"
echo "  3. 浏览器打开 http://127.0.0.1:8000 或打开「盯盘」APK 点「本机 Termux」"
echo "系统设置里把 Termux 电池优化关掉，否则切后台会被杀。"
