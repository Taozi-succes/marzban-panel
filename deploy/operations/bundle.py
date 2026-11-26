"""Authenticated, password-encrypted migration archives with strict extraction."""
import io
import os
from pathlib import Path, PurePosixPath
import tarfile

from common import MAX_BUNDLE, ToolError, canonical, digest, private_write

MAGIC=b"MARZBAN-BUNDLE-1\n"


def key(password,salt):
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        return PBKDF2HMAC(algorithm=hashes.SHA256(),length=32,salt=salt,iterations=600000).derive(password.encode())
    except ImportError:
        raise ToolError("迁移工具需要 python3-cryptography；请先安装该软件包") from None


def seal(payload,password):
    if len(password)<12:
        raise ToolError("备份口令至少 12 位，请安全保存，脚本不保存此口令")
    if len(payload)>MAX_BUNDLE:
        raise ToolError("备份超过当前工具 256 MiB 上限，需要另行安排大库迁移")
    derived_salt=os.urandom(16)
    nonce=os.urandom(12)
    secret=key(password,derived_salt)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    header=MAGIC+derived_salt+nonce
    return header+AESGCM(secret).encrypt(nonce,payload,header)


def unseal(payload,password):
    if not payload.startswith(MAGIC) or not len(MAGIC)+44<=len(payload)<=MAX_BUNDLE+100:
        raise ToolError("迁移包格式或大小无效")
    salt_start=len(MAGIC)
    salt=payload[salt_start:salt_start+16]
    nonce=payload[salt_start+16:salt_start+28]
    header=payload[:salt_start+28]
    secret=key(password,salt)
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(secret).decrypt(nonce,payload[len(header):],header)
    except InvalidTag:
        raise ToolError("备份口令错误或迁移包已损坏；没有恢复任何数据") from None


def pack(files,metadata):
    metadata=dict(metadata,format=1,files={name:digest(data) for name,data in files.items()})
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode="w:gz") as archive:
        for name,data in dict(files,**{"manifest.json":canonical(metadata)}).items():
            entry=tarfile.TarInfo(name)
            entry.size=len(data)
            entry.mode=0o600
            archive.addfile(entry,io.BytesIO(data))
    if len(output.getvalue())>MAX_BUNDLE:
        raise ToolError("迁移包过大")
    return output.getvalue()


def unpack(payload,directory):
    import json
    found={}
    total=0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload),mode="r:gz") as archive:
            for entry in archive:
                parts=PurePosixPath(entry.name).parts
                valid=entry.name in ("manifest.json","source.env.json","runtime/xray-current") or (len(parts)>=2 and parts[0]=="data")
                if not valid or ".." in parts or PurePosixPath(entry.name).is_absolute() or PurePosixPath(entry.name).as_posix()!=entry.name or '\\' in entry.name or not entry.isfile() or entry.name in found:
                    raise ToolError("迁移包包含重复、不支持或不安全的路径")
                total+=entry.size
                if total>MAX_BUNDLE or entry.size<0:
                    raise ToolError("解压数据超过 256 MiB 上限")
                found[entry.name]=archive.extractfile(entry).read()
    except (tarfile.TarError,EOFError):
        raise ToolError("迁移包无法解析") from None
    try:
        manifest=json.loads(found.pop("manifest.json"))
        if manifest["format"]!=1 or manifest["files"]!={name:digest(data) for name,data in found.items()}:
            raise ToolError("迁移包完整性校验失败")
        if not {"data/db.sqlite3","data/xray.json","source.env.json"}.issubset(found):
            raise ToolError("迁移包缺少数据库、核心配置或环境配置")
    except (ValueError,KeyError,TypeError):
        raise ToolError("迁移包清单无效") from None
    for name,data in found.items():
        private_write(Path(directory)/name,data,0o700 if name=="runtime/xray-current" else 0o600)
    return manifest
