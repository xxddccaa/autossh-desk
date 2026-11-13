from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tunnel(Base):
    __tablename__ = "tunnels"
    __table_args__ = (
        UniqueConstraint(
            "ssh_target",
            "remote_bind_host",
            "remote_port",
            name="uq_tunnel_remote_listener",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(80))
    ssh_target: Mapped[str] = mapped_column(String(255), default="root@remote.example.com")
    ssh_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_bind_host: Mapped[str] = mapped_column(String(255), default="127.0.0.1")
    remote_port: Mapped[int] = mapped_column(Integer, default=15432)
    target_host: Mapped[str] = mapped_column(
        String(255), default="service.example.com"
    )
    target_port: Mapped[int] = mapped_column(Integer, default=5432)
    identity_file: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    server_alive_interval: Mapped[int] = mapped_column(Integer, default=30)
    server_alive_count_max: Mapped[int] = mapped_column(Integer, default=3)
    connect_timeout: Mapped[int] = mapped_column(Integer, default=10)
    auto_reconnect: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_start: Mapped[bool] = mapped_column(Boolean, default=False)
    manually_stopped: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

