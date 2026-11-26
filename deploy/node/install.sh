#!/usr/bin/env bash
set -euo pipefail
umask 077

cd -- "$(dirname -- "$0")"
sha256sum --check --strict SHA256SUMS >/dev/null

if ! command -v python3 >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    for arg in "$@"; do
        if [[ $arg == --plan ]]; then
            echo '预览需要先安装 python3 和 openssl。' >&2
            exit 1
        fi
    done
    if [[ $EUID != 0 ]]; then
        echo '请使用 root 或 sudo 运行安装入口。' >&2
        exit 1
    fi
    apt-get update
    apt-get install -y python3 openssl ca-certificates
fi
exec python3 installer.py "$@"
