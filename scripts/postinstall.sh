#!/bin/bash
set -e

# 解析安装目录（兼容符号链接）
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
INSTALL_DIR="$(cd -P "$(dirname "$SOURCE")" && cd .. && pwd)"

cd "$INSTALL_DIR"
bash init.sh --npm
