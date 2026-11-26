"""Executed in an isolated helper container with the source volumes read-only."""
import json
import os
from pathlib import Path
import re
import sys

from bundle import pack
from common import MAX_BUNDLE, ToolError, canonical, digest
from database import inventory,snapshot

APP_FILES=("app/db/models.py","app/db/crud.py","app/models/user.py","app/xray/config.py",
    "app/subscription/share.py","app/db/migrations/versions/1fd4ac8350e9_explicit_inbound_grants.py")


def app_fingerprint(code=Path("/code")):
    return digest(canonical({name:digest((code/name).read_bytes()) for name in APP_FILES}))


def collect(metadata):
    data_root=Path("/var/lib/marzban").resolve()
    url=os.environ.get("SQLALCHEMY_DATABASE_URL","sqlite:///db.sqlite3")
    if not url.startswith("sqlite:///"):
        raise ToolError("当前迁移工具仅支持 SQLite，其他数据库需要独立迁移方案")
    database=Path(url[len("sqlite:///"):]).resolve()
    core=Path(os.environ.get("XRAY_JSON","/code/xray_config.json")).resolve()
    config=json.loads(core.read_text())
    files={"data/db.sqlite3":snapshot(database)}
    total=len(files["data/db.sqlite3"])
    def put(name,path):
        nonlocal total
        size=path.stat().st_size
        total+=size
        if total>MAX_BUNDLE:
            raise ToolError("未压缩数据超过 256 MiB，需另行安排大库迁移")
        files[name]=path.read_bytes()
    # Preserve all ordinary application data; explicitly captured SQLite and
    # normalized core replace their source names. Never traverse arbitrary links.
    for path in sorted(data_root.rglob("*")):
        if path.is_symlink():
            raise ToolError("数据目录含符号链接，请先整理为实际文件后备份")
        if path.is_file() and path.resolve() not in (database,core) and not path.name.endswith(("-wal","-shm","-journal")):
            put("data/"+path.relative_to(data_root).as_posix(),path)
    def assets(value):
        if isinstance(value,dict):
            for name,item in list(value.items()):
                if name in ("certificateFile","keyFile") and isinstance(item,str):
                    source=Path(item).resolve()
                    destination="data/imported-assets/"+digest(str(source).encode())[:16]+".pem"
                    put(destination,source)
                    value[name]="/var/lib/marzban/"+destination[len("data/"):]
                else:
                    assets(item)
        elif isinstance(value,list):
            for item in value: assets(item)
    assets(config)
    files["data/xray.json"]=canonical(config)
    keys=set(re.findall(r'config\([\s\n]*["\x27]([A-Z][A-Z0-9_]*)',Path('/code/config.py').read_text()))
    environment={k:os.environ[k] for k in keys if k in os.environ}
    executable=environment.get("XRAY_EXECUTABLE_PATH","/usr/local/bin/xray")
    if executable=="/usr/local/bin/xray-panel":
        put("runtime/xray-current",Path("/usr/local/bin/xray-current"))
    elif executable!="/usr/local/bin/xray":
        raise ToolError("自定义 Xray 可执行路径不在当前支持范围")
    custom=environment.get("CUSTOM_TEMPLATES_DIRECTORY","")
    if custom:
        path=Path(custom).resolve()
        if not path.is_relative_to(data_root):
            raise ToolError("自定义模板目录在数据目录以外，请先整理后备份")
    environment.update(SQLALCHEMY_DATABASE_URL="sqlite:////var/lib/marzban/db.sqlite3",XRAY_JSON="/var/lib/marzban/xray.json")
    files["source.env.json"]=canonical(environment)
    profile=inventory(raw=files["data/db.sqlite3"])
    addresses={"{SERVER_IP}"}
    addresses.update(metadata.get("source_addresses",[]))
    import sqlite3
    db=sqlite3.connect(":memory:");db.deserialize(files["data/db.sqlite3"])
    marks=",".join("?" for _ in addresses)
    where=f"COALESCE(h.is_disabled,0)=0 AND h.address IN ({marks})"
    host_rows=db.execute("SELECT COUNT(*) FROM hosts h WHERE "+where,tuple(addresses)).fetchone()[0]
    users=db.execute("SELECT COUNT(DISTINCT p.user_id) FROM hosts h JOIN allowed_inbounds_association a ON a.inbound_tag=h.inbound_tag JOIN proxies p ON p.id=a.proxy_id WHERE "+where,tuple(addresses)).fetchone()[0]
    db.close()
    metadata.update(profile=profile,app_fingerprint=app_fingerprint(),local_exits={"host_rows":host_rows,"users":users})
    return pack(files,metadata)


if __name__=="__main__":
    try:
        if len(sys.argv)>1 and sys.argv[1]=="fingerprint":
            print(app_fingerprint())
        else:
            metadata=json.loads(sys.argv[1])
            sys.stdout.buffer.write(collect(metadata))
    except (ToolError,OSError,ValueError) as error:
        print(str(error) if isinstance(error,ToolError) else "读取源配置失败，未生成备份",file=sys.stderr)
        sys.exit(1)
