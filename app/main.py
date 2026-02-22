"""
FastAPI application — serves the web UI, REST API, and HLS segments.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import database
from .stream_manager import STREAMS_DIR, StreamManager

logger = logging.getLogger(__name__)

# ── Lifespan ────────────────────────────────────────────────────────
manager: StreamManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    database.init_db()
    manager = StreamManager()
    logger.info("StreamManager initialised – database ready")
    await manager.restore_streams()
    yield
    logger.info("Shutting down – stopping all streams")
    manager.stop_all()


app = FastAPI(title="CreAIta – Stream Viewer", lifespan=lifespan)

# ── Serve HLS segments ─────────────────────────────────────────────
STREAMS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/streams", StaticFiles(directory=str(STREAMS_DIR)), name="hls_streams")

# ── Serve static frontend assets ───────────────────────────────────
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── API models ──────────────────────────────────────────────────────
class AddStreamRequest(BaseModel):
    source_url: str
    name: str | None = None  # Optional - will be derived from stream if not provided


class StreamResponse(BaseModel):
    id: str
    name: str
    source_url: str
    status: str
    playlist_url: str
    error_message: str = ""


# ── REST endpoints ──────────────────────────────────────────────────
def get_user_from_session(session_id: str | None, response: Response) -> database.User:
    """Get or create user from session cookie."""
    user = database.get_or_create_user(session_id)
    if not session_id or session_id != user.session_id:
        # Set cookie for 30 days
        response.set_cookie(
            key="session_id",
            value=user.session_id,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )
    return user


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/streams")
async def list_streams(
    response: Response, session_id: str | None = Cookie(default=None)
) -> list[dict]:
    user = get_user_from_session(session_id, response)
    return manager.list_streams(user.id)


@app.post("/api/streams", status_code=201)
async def add_stream(
    req: AddStreamRequest, response: Response, session_id: str | None = Cookie(default=None)
) -> dict:
    user = get_user_from_session(session_id, response)
    info = await manager.add_stream_async(user_id=user.id, source_url=req.source_url, name=req.name)
    managed = manager.get_stream(user.id, info.id)
    return {
        "id": info.id,
        "name": info.name,
        "source_url": info.source_url,
        "status": info.status,
        "playlist_url": managed.playlist_url,
    }


@app.delete("/api/streams/{stream_id}")
async def remove_stream(
    stream_id: str, response: Response, session_id: str | None = Cookie(default=None)
):
    user = get_user_from_session(session_id, response)
    if not manager.remove_stream(user.id, stream_id):
        raise HTTPException(status_code=404, detail="Stream not found")
    return {"detail": "Stream removed"}
