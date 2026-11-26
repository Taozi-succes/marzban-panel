"""Seed only the disposable Docker test database, then start the real panel."""
import os
import subprocess
import sys
sys.path.insert(0, "/code")

assert os.environ["SQLALCHEMY_DATABASE_URL"] == "sqlite:////data/db.sqlite3"
subprocess.run(["alembic", "upgrade", "head"], check=True)
from app.db import GetDB, crud
from app.models.admin import AdminCreate
from app.models.user import UserCreate

with GetDB() as db:
    admin = crud.create_admin(db, AdminCreate(username="fixture_admin", password="fixture_password", is_sudo=True))
    user = crud.create_user(db, UserCreate(username="fixture_existing", proxies={"vless": {}},
        inbounds={"vless": ["existing"]}, data_limit=104857600, expire=2100000000), admin=admin)
    user.used_traffic = 37
    db.commit()

os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"])
