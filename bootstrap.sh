#!/usr/bin/env bash
# Keep the final main invocation last so an incomplete download cannot start provisioning.
set -euo pipefail

main() (
    umask 077
    if (( EUID != 0 )); then
        echo '请使用 root 运行；可先执行 sudo -i。' >&2
        exit 1
    fi
    for tool in curl python3 openssl sha256sum; do
        if ! command -v "$tool" >/dev/null 2>&1; then
            apt-get update
            apt-get install -y curl ca-certificates python3 openssl coreutils
            break
        fi
    done
    local_stage=$(mktemp -d -t marzban-node.XXXXXXXX)
    trap 'rm -rf -- "$local_stage"' EXIT
    curl --fail --silent --show-error --location --max-time 180 \
        --proto '=https' --proto-redir '=https' --max-filesize 67108864 \
        'https://codeload.github.com/Taozi-succes/marzban-node-installer/tar.gz/refs/tags/v1.0.1' \
        --output "$local_stage/source.tgz"
    python3 - "$local_stage/source.tgz" "$local_stage/installer" <<'PY'
import hashlib
from pathlib import Path, PurePosixPath
import sys
import tarfile

archive_path, directory = Path(sys.argv[1]), Path(sys.argv[2])
directory.mkdir(mode=0o700)
files = {"installer.py", "api.py", "plan.py", "system.py", "probe.py", "release.py", "install.sh", "SHA256SUMS"}
seen = set()
with tarfile.open(archive_path, "r:gz") as archive:
    for entry in archive:
        parts = PurePosixPath(entry.name).parts
        if ".." in parts or PurePosixPath(entry.name).is_absolute():
            raise SystemExit("安装包路径不正确")
        if len(parts) != 4 or parts[1:3] != ("deploy", "node") or parts[-1] not in files:
            continue
        name = parts[-1]
        if name in seen or not entry.isfile() or entry.size > 1024 * 1024:
            raise SystemExit("安装包包含重复或不支持的文件")
        (directory/name).write_bytes(archive.extractfile(entry).read())
        (directory/name).chmod(0o600)
        seen.add(name)
if seen != files:
    raise SystemExit("安装包缺少必要文件")
checks = {}
for line in (directory/"SHA256SUMS").read_text().splitlines():
    fields = line.split()
    if len(fields) != 2 or fields[1] in checks or fields[1] not in files - {"SHA256SUMS"}:
        raise SystemExit("安装包校验清单不正确")
    checks[fields[1]] = fields[0]
if set(checks) != files - {"SHA256SUMS"} or any(
    hashlib.sha256((directory/name).read_bytes()).hexdigest() != digest for name, digest in checks.items()
):
    raise SystemExit("安装包校验失败")
PY
    bash "$local_stage/installer/install.sh" "$@"
)

main "$@"
