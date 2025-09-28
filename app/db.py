from __future__ import annotations

from contextlib import contextmanager
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.engine import Engine
from .config import settings
import os


class Base(DeclarativeBase):
    pass


def ensure_dirs():
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        # path like sqlite:///./data/app.db
        path = url.split("sqlite:///")[-1]
        dir_ = os.path.dirname(os.path.abspath(path))
        if dir_ and not os.path.exists(dir_):
            os.makedirs(dir_, exist_ok=True)


ensure_dirs()
engine: Engine = create_engine(settings.DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    from . import models  # noqa
    Base.metadata.create_all(bind=engine)
    create_fts_objects()


def create_fts_objects():
    """Create FTS5 virtual table and triggers for messages if not exists."""
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        # Create FTS5 table
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    content_text, sender_name, talker_name, content='messages', content_rowid='id'
                );
                """
            )
        )
        # Triggers
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content_text, sender_name, talker_name)
                    VALUES (new.id, new.content_text, new.sender_name, new.talker_name);
                END;
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content_text, sender_name, talker_name)
                    VALUES('delete', old.id, old.content_text, old.sender_name, old.talker_name);
                END;
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content_text, sender_name, talker_name)
                    VALUES('delete', old.id, old.content_text, old.sender_name, old.talker_name);
                    INSERT INTO messages_fts(rowid, content_text, sender_name, talker_name)
                    VALUES (new.id, new.content_text, new.sender_name, new.talker_name);
                END;
                """
            )
        )

