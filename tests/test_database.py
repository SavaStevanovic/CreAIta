"""
Tests for database module.
"""

import tempfile
from pathlib import Path

import pytest

from app import database


@pytest.fixture
def temp_db(monkeypatch):
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        monkeypatch.setattr(database, "DB_PATH", db_path)
        database.init_db()
        yield db_path


def test_create_user(temp_db):
    """Test user creation."""
    user = database.create_user()
    assert user.id > 0
    assert len(user.session_id) == 32  # UUID hex is 32 chars
    assert user.created_at > 0


def test_get_user_by_session(temp_db):
    """Test retrieving user by session ID."""
    user = database.create_user()
    retrieved = database.get_user_by_session(user.session_id)
    assert retrieved is not None
    assert retrieved.id == user.id
    assert retrieved.session_id == user.session_id


def test_get_or_create_user_existing(temp_db):
    """Test get_or_create with existing user."""
    user = database.create_user()
    retrieved = database.get_or_create_user(user.session_id)
    assert retrieved.id == user.id


def test_get_or_create_user_new(temp_db):
    """Test get_or_create with new user."""
    user = database.get_or_create_user()
    assert user.id > 0


def test_save_and_get_stream(temp_db):
    """Test saving and retrieving streams."""
    user = database.create_user()

    database.save_stream(
        user_id=user.id,
        stream_id="test123",
        name="Test Stream",
        source_url="rtsp://example.com/stream",
        status="running",
    )

    streams = database.get_user_streams(user.id)
    assert len(streams) == 1
    assert streams[0].stream_id == "test123"
    assert streams[0].name == "Test Stream"
    assert streams[0].status == "running"


def test_update_stream(temp_db):
    """Test updating an existing stream."""
    user = database.create_user()

    # Create stream
    database.save_stream(
        user_id=user.id,
        stream_id="test123",
        name="Test Stream",
        source_url="rtsp://example.com/stream",
        status="starting",
    )

    # Update stream
    database.save_stream(
        user_id=user.id,
        stream_id="test123",
        name="Updated Stream",
        source_url="rtsp://example.com/stream",
        status="running",
    )

    streams = database.get_user_streams(user.id)
    assert len(streams) == 1
    assert streams[0].name == "Updated Stream"
    assert streams[0].status == "running"


def test_delete_stream(temp_db):
    """Test deleting a stream."""
    user = database.create_user()

    database.save_stream(
        user_id=user.id,
        stream_id="test123",
        name="Test Stream",
        source_url="rtsp://example.com/stream",
        status="running",
    )

    deleted = database.delete_stream(user.id, "test123")
    assert deleted is True

    streams = database.get_user_streams(user.id)
    assert len(streams) == 0


def test_multiple_users_separate_streams(temp_db):
    """Test that different users have separate streams."""
    user1 = database.create_user()
    user2 = database.create_user()

    database.save_stream(
        user_id=user1.id,
        stream_id="stream1",
        name="User 1 Stream",
        source_url="rtsp://example.com/1",
        status="running",
    )

    database.save_stream(
        user_id=user2.id,
        stream_id="stream2",
        name="User 2 Stream",
        source_url="rtsp://example.com/2",
        status="running",
    )

    user1_streams = database.get_user_streams(user1.id)
    user2_streams = database.get_user_streams(user2.id)

    assert len(user1_streams) == 1
    assert len(user2_streams) == 1
    assert user1_streams[0].stream_id == "stream1"
    assert user2_streams[0].stream_id == "stream2"
