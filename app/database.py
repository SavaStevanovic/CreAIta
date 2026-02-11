"""
Database module for per-user stream state management.

Uses SQLite for simplicity and no external dependencies.
Each user gets a unique session ID stored in a cookie.
"""

import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "streams" / "creaita.db"


@dataclass
class User:
    """Represents a user session."""

    id: int
    session_id: str
    created_at: float


@dataclass
class StreamRecord:
    """Database record for a stream."""

    id: int
    user_id: int
    stream_id: str
    name: str
    source_url: str
    status: str
    error_message: str
    is_platform_url: bool
    is_vod: bool
    created_at: float


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS streams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stream_id TEXT NOT NULL,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                is_platform_url BOOLEAN DEFAULT 0,
                is_vod BOOLEAN DEFAULT 0,
                created_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, stream_id)
            )
        """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON users(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_streams ON streams(user_id)")


def create_user(session_id: str | None = None) -> User:
    """Create a new user with a unique session ID."""
    if not session_id:
        session_id = uuid.uuid4().hex

    created_at = time.time()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO users (session_id, created_at) VALUES (?, ?)", (session_id, created_at)
        )
        user_id = cursor.lastrowid

    return User(id=user_id, session_id=session_id, created_at=created_at)


def get_user_by_session(session_id: str) -> User | None:
    """Get user by session ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, session_id, created_at FROM users WHERE session_id = ?", (session_id,)
        ).fetchone()

    if row:
        return User(id=row["id"], session_id=row["session_id"], created_at=row["created_at"])
    return None


def get_or_create_user(session_id: str | None = None) -> User:
    """Get existing user or create a new one."""
    if session_id:
        user = get_user_by_session(session_id)
        if user:
            return user
    return create_user(session_id)


def save_stream(
    user_id: int,
    stream_id: str,
    name: str,
    source_url: str,
    status: str,
    error_message: str = "",
    is_platform_url: bool = False,
    is_vod: bool = False,
) -> None:
    """Save or update a stream for a user."""
    created_at = time.time()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO streams (user_id, stream_id, name, source_url, status,
                                error_message, is_platform_url, is_vod, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, stream_id) DO UPDATE SET
                name = excluded.name,
                status = excluded.status,
                error_message = excluded.error_message,
                is_platform_url = excluded.is_platform_url,
                is_vod = excluded.is_vod
        """,
            (
                user_id,
                stream_id,
                name,
                source_url,
                status,
                error_message,
                is_platform_url,
                is_vod,
                created_at,
            ),
        )


def get_user_streams(user_id: int) -> list[StreamRecord]:
    """Get all streams for a user."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, stream_id, name, source_url, status, error_message,
                   is_platform_url, is_vod, created_at
            FROM streams
            WHERE user_id = ?
            ORDER BY created_at DESC
        """,
            (user_id,),
        ).fetchall()

    return [
        StreamRecord(
            id=row["id"],
            user_id=row["user_id"],
            stream_id=row["stream_id"],
            name=row["name"],
            source_url=row["source_url"],
            status=row["status"],
            error_message=row["error_message"],
            is_platform_url=bool(row["is_platform_url"]),
            is_vod=bool(row["is_vod"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


def delete_stream(user_id: int, stream_id: str) -> bool:
    """Delete a stream for a user."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM streams WHERE user_id = ? AND stream_id = ?", (user_id, stream_id)
        )
        return cursor.rowcount > 0


def cleanup_old_sessions(days: int = 30) -> int:
    """Delete users and their streams that are older than specified days."""
    cutoff = time.time() - (days * 86400)

    with get_db() as conn:
        cursor = conn.execute("DELETE FROM users WHERE created_at < ?", (cutoff,))
        return cursor.rowcount
