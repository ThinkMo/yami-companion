from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    url = database_url or settings.database_url
    is_sqlite = url.startswith("sqlite")
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    return create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=not is_sqlite,
        future=True,
    )


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(target_engine=None) -> None:
    eng = target_engine or engine
    Base.metadata.create_all(bind=eng)
    try:
        insp = inspect(eng)
        is_pg = eng.dialect.name == "postgresql"
        bool_default = "BOOLEAN DEFAULT FALSE" if is_pg else "BOOLEAN DEFAULT 0"
        dt_type = "TIMESTAMP WITH TIME ZONE" if is_pg else "DATETIME"
        tables = set(insp.get_table_names())
        if "pet_profiles" in tables:
            pcols = {c["name"] for c in insp.get_columns("pet_profiles")}
            palters = [
                ("is_runaway", bool_default),
                ("last_action_kind", "VARCHAR(24) DEFAULT 'idle'"),
                ("last_care_at", dt_type),
                ("last_decay_at", dt_type),
            ]
            with eng.begin() as conn:
                for col_name, col_def in palters:
                    if col_name not in pcols:
                        conn.execute(text(f"ALTER TABLE pet_profiles ADD COLUMN {col_name} {col_def}"))
        if "students" in tables:
            scols = {c["name"] for c in insp.get_columns("students")}
            salters = [
                ("pet_jump_seq", "INTEGER DEFAULT 0"),
                ("last_desk_check_at_desk", "BOOLEAN"),
                ("last_desk_check_summary", "TEXT"),
                ("last_desk_check_at", dt_type),
            ]
            with eng.begin() as conn:
                for col_name, col_def in salters:
                    if col_name not in scols:
                        conn.execute(text(f"ALTER TABLE students ADD COLUMN {col_name} {col_def}"))
        if "devices" in tables:
            dcols = {c["name"] for c in insp.get_columns("devices")}
            if "desk_check_seq" not in dcols:
                with eng.begin() as conn:
                    conn.execute(text("ALTER TABLE devices ADD COLUMN desk_check_seq INTEGER DEFAULT 0"))
        if "families" in tables:
            fcols = {c["name"] for c in insp.get_columns("families")}
            falters = [
                ("dingtalk_app_id", "VARCHAR(128)"),
                ("dingtalk_app_secret", "VARCHAR(256)"),
                ("dingtalk_class_group_id", "VARCHAR(128)"),
            ]
            with eng.begin() as conn:
                for col_name, col_def in falters:
                    if col_name not in fcols:
                        conn.execute(text(f"ALTER TABLE families ADD COLUMN {col_name} {col_def}"))
    except Exception:
        pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
