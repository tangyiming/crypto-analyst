#!/usr/bin/env bash
# 编译 Android debug APK（套壳打开电脑上的 Web 盯盘）。
#
# 用法：./scripts/build-apk.sh
# 产物：dist/crypto-analyst.apk
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
if [[ ! -d "$ANDROID_HOME/platforms" ]]; then
  echo "未找到 Android SDK（$ANDROID_HOME）。请安装 Android Studio 或设置 ANDROID_HOME。" >&2
  exit 1
fi

JAVA_BIN="${JAVA_HOME:-}/bin/java"
if [[ ! -x "$JAVA_BIN" ]]; then
  if [[ -x /opt/homebrew/opt/openjdk@17/bin/java ]]; then
    export JAVA_HOME="/opt/homebrew/opt/openjdk@17"
  elif [[ -x /opt/homebrew/opt/openjdk@21/bin/java ]]; then
    export JAVA_HOME="/opt/homebrew/opt/openjdk@21"
  fi
fi

"$ROOT/.venv/bin/python" "$ROOT/scripts/generate_favicon.py" >/dev/null

GRADLE_VER="8.9"
GRADLE_DIR="${GRADLE_USER_HOME:-$HOME/.gradle}/wrapper/dists/gradle-${GRADLE_VER}-bin"
mkdir -p "$ROOT/.cache/gradle"
GRADLE_ZIP="$ROOT/.cache/gradle/gradle-${GRADLE_VER}-bin.zip"
if [[ ! -x "$ROOT/.cache/gradle/gradle-${GRADLE_VER}/bin/gradle" ]]; then
  if [[ ! -f "$GRADLE_ZIP" ]]; then
    echo "下载 Gradle ${GRADLE_VER}…"
    curl -fsSL "https://services.gradle.org/distributions/gradle-${GRADLE_VER}-bin.zip" -o "$GRADLE_ZIP"
  fi
  rm -rf "$ROOT/.cache/gradle/gradle-${GRADLE_VER}"
  unzip -q "$GRADLE_ZIP" -d "$ROOT/.cache/gradle"
fi
GRADLE="$ROOT/.cache/gradle/gradle-${GRADLE_VER}/bin/gradle"

mkdir -p "$ROOT/android"
cat > "$ROOT/android/local.properties" <<EOF
sdk.dir=$ANDROID_HOME
EOF

echo "编译 debug APK…"
(cd "$ROOT/android" && "$GRADLE" :app:assembleDebug --no-daemon)

APK_SRC="$ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
mkdir -p "$ROOT/dist"
cp "$APK_SRC" "$ROOT/dist/crypto-analyst.apk"
echo "APK: $ROOT/dist/crypto-analyst.apk"
echo "安装：把文件发到手机，允许未知来源后打开安装。"
echo "电脑需先：./scripts/run-web.sh --lan"
