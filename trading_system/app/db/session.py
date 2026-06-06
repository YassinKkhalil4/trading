from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from trading_system.app.core.config import get_settings


settings = get_settings()


def build_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = build_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
