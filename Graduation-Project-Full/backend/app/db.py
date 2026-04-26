from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import settings


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def db_session() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

