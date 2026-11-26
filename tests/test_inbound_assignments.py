"""Integration regression for the stock 0.8.4 migration, subscription, and Xray users."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from uuid import uuid4


def inbound(tag, port):
    return {"tag": tag, "listen": "127.0.0.1", "port": port, "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"}}


def check(directory):
    database = directory / "db.sqlite3"
    conf = directory / "xray.json"
    config = {"inbounds": [inbound("A", 10001), inbound("B", 10002)],
              "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]}
    conf.write_text(json.dumps(config))
    os.environ.update(SQLALCHEMY_DATABASE_URL=f"sqlite:///{database}", XRAY_JSON=str(conf))
    subprocess.run(["alembic", "upgrade", "2b231de97dc3"], check=True)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO users(id,username,status,used_traffic,expire,data_limit) VALUES(1,'fixture_old','active',37,2100000000,1000)")
        db.execute("INSERT INTO proxies(id,user_id,type,settings) VALUES(1,1,'VLESS',?)",
                   (json.dumps({"id": str(uuid4()), "flow": ""}),))
        db.execute("INSERT INTO inbounds(tag) VALUES('B')")
        db.execute("INSERT INTO exclude_inbounds_association(proxy_id,inbound_tag) VALUES(1,'B')")
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    from app import xray
    from app.db import GetDB, crud
    from app.models.user import UserCreate, UserModify, UserResponse
    from app.xray import XRayConfig

    def users_by_tag():
        return {i["tag"]: {c["email"] for c in i["settings"]["clients"]}
                for i in xray.config.include_db_users()["inbounds"] if i["protocol"] == "vless"}

    with GetDB() as db:
        old = crud.get_user(db, "fixture_old")
        assert old.inbounds == {"vless": ["A"]} and (old.used_traffic, old.expire, old.data_limit) == (37, 2100000000, 1000)
        config["inbounds"].append(inbound("C", 10003))
        conf.write_text(json.dumps(config))
        xray.config = XRayConfig(config)
        xray.hosts.update()
        assert users_by_tag() == {"A": {"1.fixture_old"}, "B": set(), "C": set()}
        assert len(UserResponse.model_validate(old).links) == 1
        blank = crud.create_user(db, UserCreate(username="fixture_blank", status="active", proxies={}))
        assert UserResponse.model_validate(blank).links == []
        user = crud.create_user(db, UserCreate(username="fixture_new", status="active", proxies={"vless": {}}))
        assert user.inbounds == {"vless": []} and UserResponse.model_validate(user).links == []
        user = crud.update_user(db, user, UserModify(inbounds={"vless": ["C"]}))
        assert user.inbounds == {"vless": ["C"]} and len(UserResponse.model_validate(user).links) == 1
        assert users_by_tag()["C"] == {f"{user.id}.fixture_new"}
        ident = user.proxies[0].settings["id"]
        crud.update_user(db, user, UserModify(note="untouched grants"))
        assert user.inbounds == {"vless": ["C"]}
        crud.update_user(db, user, UserModify(inbounds={"vless": []}))
        assert users_by_tag()["C"] == set() and UserResponse.model_validate(user).links == []
        assert user.proxies[0].settings["id"] == ident
        crud.update_user(db, user, UserModify(inbounds={"vless": ["C"]}))
        crud.update_user(db, user, UserModify(inbounds={}))
        assert user.inbounds == {"vless": []}
        crud.update_user(db, blank, UserModify(proxies={"vless": {}}, inbounds={"vless": ["B"]}))
        assert blank.inbounds == {"vless": ["B"]}
        crud.update_user(db, blank, UserModify(proxies={}, inbounds={}))
        assert UserResponse.model_validate(blank).links == []
        crud.update_user(db, user, UserModify(inbounds={"vless": ["C"]}))
        removed = dict(config, inbounds=config["inbounds"][:2])
        crud.remove_retired_inbound_grants(db, XRayConfig(removed))
        db.expire_all()
        assert users_by_tag()["C"] == set() and user.inbounds == {"vless": []}
    subprocess.run(["alembic", "downgrade", "2b231de97dc3"], check=True)
    with sqlite3.connect(database) as db:
        assert set(db.execute("SELECT inbound_tag FROM exclude_inbounds_association WHERE proxy_id=1")) == {("B",), ("C",)}
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    assert users_by_tag() == {"A": {"1.fixture_old"}, "B": set(), "C": set()}
    print("Verified legacy assignments, new inbound, empty account, grant, revoke, core, subscription, and rollback.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        check(Path(tmp))
