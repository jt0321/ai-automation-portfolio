"""db/session.py — SQLAlchemy session factory."""
import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_engine = None
_Session = None


def get_session():
    global _engine, _Session
    if _engine is None:
        url = os.environ["DATABASE_URL"]
        _engine = create_engine(url, pool_pre_ping=True)
        _Session = sessionmaker(bind=_engine)
    return _Session()


@contextmanager
def session_scope():
    """Yield a session and always close it, so short-lived requests
    (each API call, each ingest step) don't leak idle-in-transaction
    connections that can end up blocking DDL/migrations."""
    session = get_session()
    try:
        yield session
    finally:
        session.close()
