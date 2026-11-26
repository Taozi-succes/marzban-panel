"""Read-only database inventories used to prove a restore preserves its source."""
import base64
from pathlib import Path
import sqlite3

from common import ToolError, canonical, digest

SCHEMA = "1fd4ac8350e9"


def readonly(path):
    connection=sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def snapshot(path):
    with readonly(path) as source, sqlite3.connect(":memory:") as target:
        source.backup(target)
        return target.serialize()


def json_cell(value):
    return {"bytes":base64.b64encode(value).decode()} if isinstance(value,bytes) else value


def inventory(path=None, raw=None):
    connection=readonly(path) if raw is None else sqlite3.connect(":memory:")
    if raw is not None:
        connection.deserialize(raw)
        connection.execute("PRAGMA query_only=ON")
    try:
      with connection as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ToolError("SQLite 完整性检查失败")
        tables=sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        required={"users","admins","proxies","hosts","nodes","jwt","tls","allowed_inbounds_association","alembic_version"}
        if not required.issubset(tables):
            raise ToolError("数据库缺少定制面板所需表，不支持直接导入官方原版数据库")
        revision=db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if revision != SCHEMA:
            raise ToolError("数据库迁移版本不匹配；请先单独完成版本兼容演练")
        profiles={}
        identities={}
        node_statuses=[]
        for table in tables:
            columns=[row[1] for row in db.execute('PRAGMA table_info("'+table.replace('"','""')+'")')]
            rows=[[json_cell(value) for value in row] for row in db.execute('SELECT * FROM "'+table.replace('"','""')+'"')]
            if table == "nodes":
                ident,status=columns.index("id"),columns.index("status")
                node_statuses=[{"id":row[ident],"status":row[status]} for row in rows]
                for row in rows:
                    row[status]="<managed-runtime-status>"
            payload=sorted(canonical(row).decode() for row in rows)
            profiles[table]={"rows":len(rows),"sha256":digest(canonical([columns,payload]))}
            if table in ("jwt","tls"):
                identities[table]=payload
        return {"schema":revision,"tables":profiles,"identity":digest(canonical(identities)),"node_statuses":node_statuses}
    finally:
        connection.close()


def same_data(expected, actual):
    if expected["schema"] != actual["schema"] or expected["tables"] != actual["tables"] or expected["identity"] != actual["identity"]:
        raise ToolError("恢复校验失败：账号、权限、计量、身份材料或其他数据与备份不一致")


def disable_nodes(path):
    with sqlite3.connect(path) as db:
        db.execute("UPDATE nodes SET status='disabled'")


def restore_node_statuses(path, rows):
    with sqlite3.connect(path) as db:
        for row in rows:
            db.execute("UPDATE nodes SET status=? WHERE id=?",(row["status"],row["id"]))


def local_exit_report(path, addresses):
    with readonly(path) as db:
        marks=",".join("?" for _ in addresses)
        where=f"COALESCE(h.is_disabled,0)=0 AND h.address IN ({marks})"
        hosts=db.execute("SELECT COUNT(*) FROM hosts h WHERE "+where,addresses).fetchone()[0]
        users=db.execute("SELECT COUNT(DISTINCT p.user_id) FROM hosts h JOIN allowed_inbounds_association a ON a.inbound_tag=h.inbound_tag JOIN proxies p ON p.id=a.proxy_id WHERE "+where,addresses).fetchone()[0]
    return {"host_rows":hosts,"users":users}
