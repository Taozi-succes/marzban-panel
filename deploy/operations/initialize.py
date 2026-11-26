"""Run inside the panel image; administrator password arrives through stdin."""
import json
import sys

sys.path.insert(0,"/code")
from app.db import GetDB,crud
from app.models.admin import AdminCreate
from app.dependencies import validate_admin
from app.db.models import User,Node

payload=json.load(sys.stdin)
with GetDB() as db:
    if db.query(User).first() or db.query(Node).first():
        raise SystemExit('数据库已有用户或节点，拒绝作为全新面板初始化')
    existing=crud.get_admin(db,payload["username"])
    if existing:
        if not existing.is_sudo or not validate_admin(db,payload["username"],payload["password"]):
            raise SystemExit("管理员已存在且密码不匹配，不会覆盖密码")
    else:
        crud.create_admin(db,AdminCreate(username=payload["username"],password=payload["password"],is_sudo=True))
print("管理员已创建")
