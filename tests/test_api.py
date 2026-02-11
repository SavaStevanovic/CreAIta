"""
Tests for API endpoints.
"""

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.stream_manager import StreamManager


@pytest.fixture(autouse=True)
def setup_db(monkeypatch, tmp_path):
    """Setup test database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db()

    # Initialize manager for tests
    global manager
    if manager is None:
        from app import main

        main.manager = StreamManager()

    yield

    # Cleanup
    if main.manager:
        main.manager.stop_all()


@pytest.fixture
def client():
    """Create a test client."""
    with TestClient(app) as client:
        yield client


def test_index_page(client):
    """Test that index page loads."""
    response = client.get("/")
    assert response.status_code == 200
    assert "CreAIta" in response.text


def test_list_streams_empty(client):
    """Test listing streams when none exist."""
    response = client.get("/api/streams")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_add_stream_without_name(client):
    """Test adding a stream without providing a name."""
    response = client.post("/api/streams", json={"source_url": "rtsp://example.com/test"})
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert "name" in data
    assert "status" in data
    assert data["source_url"] == "rtsp://example.com/test"


def test_add_stream_with_name(client):
    """Test adding a stream with a custom name."""
    response = client.post(
        "/api/streams", json={"source_url": "rtsp://example.com/test", "name": "My Custom Stream"}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "My Custom Stream"


def test_session_cookie_set(client):
    """Test that session cookie is set on first request."""
    response = client.get("/api/streams")
    assert response.status_code == 200
    assert "session_id" in response.cookies


def test_session_persistence(client):
    """Test that streams are per-session."""
    # First session
    response1 = client.post("/api/streams", json={"source_url": "rtsp://example.com/stream1"})

    # Second session (new client without cookies)
    client2 = TestClient(app)
    response2 = client2.get("/api/streams")

    # First session should have 1 stream
    # Second session should have 0 streams
    assert response1.status_code == 201
    assert len(response2.json()) == 0
