from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Tunnel


DEFAULT_TUNNEL = {
    "name": "示例隧道",
    "ssh_target": "root@remote.example.com",
    "ssh_port": None,
    "remote_bind_host": "127.0.0.1",
    "remote_port": 15432,
    "target_host": "service.example.com",
    "target_port": 5432,
    "identity_file": None,
    "server_alive_interval": 30,
    "server_alive_count_max": 3,
    "connect_timeout": 10,
    "auto_reconnect": True,
    "auto_start": False,
}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 10},
        )
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            columns = connection.exec_driver_sql("PRAGMA table_info(tunnels)").all()
            if not any(column[1] == "manually_stopped" for column in columns):
                connection.exec_driver_sql(
                    "ALTER TABLE tunnels ADD COLUMN manually_stopped BOOLEAN NOT NULL DEFAULT 0"
                )
        if path.exists():
            os.chmod(path, 0o600)
        with self.session_factory() as session:
            if session.scalar(select(Tunnel.id).limit(1)) is None:
                session.add(Tunnel(**DEFAULT_TUNNEL))
                session.commit()

    def session(self) -> Session:
        return self.session_factory()

