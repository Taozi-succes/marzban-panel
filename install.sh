#!/usr/bin/env bash
# This is copied to the two public entry points with a fixed CLI name.
set -euo pipefail
main() (
  umask 077
  if (( EUID != 0 )); then echo '请先 sudo -i 切换 root。' >&2; exit 1; fi
  if ! command -v python3 >/dev/null; then
    apt-get update
    apt-get install -y python3 ca-certificates
  fi
  local_stage=$(mktemp -d -t marzban-panel.XXXXXXXX)
  trap 'rm -rf -- "$local_stage"' EXIT
  curl --fail --silent --show-error --location --connect-timeout 10 --max-time 180 \
    --proto '=https' --proto-redir '=https' --max-filesize 67108864 \
    'https://codeload.github.com/Taozi-succes/marzban-panel/tar.gz/refs/tags/v1.1.0' \
    --output "$local_stage/source.tgz"
  python3 - "$local_stage/source.tgz" "$local_stage/toolkit" <<'PY'
import hashlib
from pathlib import Path,PurePosixPath
import sys,tarfile
source,target=Path(sys.argv[1]),Path(sys.argv[2]);target.mkdir(mode=0o700)
files={}
with tarfile.open(source,"r:gz") as archive:
    for entry in archive:
        parts=PurePosixPath(entry.name).parts
        if ".." in parts or PurePosixPath(entry.name).is_absolute():raise SystemExit("包路径无效")
        if len(parts)!=4 or parts[1:3]!=("deploy","operations"):continue
        name=parts[-1]
        if name!="SHA256SUMS" and not (name.endswith(".py") and name.replace("_","").replace(".","").isalnum()):continue
        if not entry.isfile() or entry.size>1024*1024 or name in files:raise SystemExit("包文件无效")
        files[name]=archive.extractfile(entry).read()
checks={}
for line in files.get("SHA256SUMS",b"").decode().splitlines():
    parts=line.split()
    if len(parts)!=2 or parts[1] in checks:raise SystemExit("清单无效")
    checks[parts[1]]=parts[0]
if set(checks)!=set(files)-{"SHA256SUMS"} or not {"panel.py","migrate.py","common.py"}.issubset(files):raise SystemExit("包文件不完整")
if any(hashlib.sha256(files[name]).hexdigest()!=value for name,value in checks.items()):raise SystemExit("包完整性校验失败")
for name,value in files.items():(target/name).write_bytes(value)
PY
  python3 "$local_stage/toolkit/panel.py" "$@"
)
main "$@"
